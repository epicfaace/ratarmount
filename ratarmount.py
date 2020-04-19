#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import bisect
import collections
import io
import itertools
import json
import os
import pprint
import re
import sqlite3
import stat
import sys
import tarfile
import tempfile
import time
import traceback
from timeit import default_timer as timer

try:
    import indexed_bzip2
    from indexed_bzip2 import IndexedBzip2File
    hasBzip2Support = True
except ImportError:
    hasBzip2Support = False

try:
    import indexed_gzip
    from indexed_gzip import IndexedGzipFile
    hasGzipSupport = True
except ImportError:
    hasGzipSupport = False

import fuse


__version__ = '0.5.0'

printDebug = 1

def overrides( parentClass ):
    def overrider( method ):
        assert method.__name__ in dir( parentClass )
        return method
    return overrider

class ProgressBar:
    def __init__( self, maxValue ):
        self.maxValue = maxValue
        self.lastUpdateTime = time.time()
        self.lastUpdateValue = 0
        self.updateInterval = 2 # seconds
        self.creationTime = time.time()

    def update( self, value ):
        if self.lastUpdateTime is not None and ( time.time() - self.lastUpdateTime ) < self.updateInterval:
            return

        # Use whole interval since start to estimate time
        eta1 = int( ( time.time() - self.creationTime ) / value * ( self.maxValue - value ) )
        # Use only a shorter window interval to estimate time.
        # Accounts better for higher speeds in beginning, e.g., caused by caching effects.
        # However, this estimate might vary a lot while the other one stabilizes after some time!
        eta2 = int( ( time.time() - self.lastUpdateTime ) / ( value - self.lastUpdateValue ) * ( self.maxValue - value ) )
        print( "Currently at position {} of {} ({:.2f}%). "
               "Estimated time remaining with current rate: {} min {} s, with average rate: {} min {} s."
               .format( value, self.maxValue, value / self.maxValue * 100.,
                        eta2 // 60, eta2 % 60,
                        eta1 // 60, eta1 % 60 ),
               flush = True )

        self.lastUpdateTime = time.time()
        self.lastUpdateValue = value

class StenciledFile(io.BufferedIOBase):
    """A file abstraction layer giving a stenciled view to an underlying file."""

    def __init__(self, fileobj, stencils):
        """
        stencils: A list tuples specifying the offset and length of the underlying file to use.
                  The order of these tuples will be kept.
                  The offset must be non-negative and the size must be positive.

        Examples:
            stencil = [(5,7)]
                Makes a new 7B sized virtual file starting at offset 5 of fileobj.
            stencil = [(0,3),(5,3)]
                Make a new 6B sized virtual file containing bytes [0,1,2,5,6,7] of fileobj.
            stencil = [(0,3),(0,3)]
                Make a 6B size file containing the first 3B of fileobj twice concatenated together.
        """

        self.fileobj = fileobj
        self.offsets = [ x[0] for x in stencils ]
        self.sizes   = [ x[1] for x in stencils ]

        # Calculate cumulative sizes
        self.cumsizes = [ 0 ]
        for offset, size in stencils:
            assert offset >= 0
            assert size > 0
            self.cumsizes.append( self.cumsizes[-1] + size )

        # Seek to the first stencil offset in the underlying file so that "read" will work out-of-the-box
        self.seek( 0 )

    def _findStencil( self, offset ):
        """
        Return index to stencil where offset belongs to. E.g., for stencils [(3,5),(8,2)], offsets 0 to
        and including 4 will still be inside stencil (3,5), i.e., index 0 will be returned. For offset 6,
        index 1 would be returned because it now is in the second contiguous region / stencil.
        """
        # bisect_left( value ) gives an index for a lower range: value < x for all x in list[0:i]
        # Because value >= 0 and list starts with 0 we can therefore be sure that the returned i>0
        # Consider the stencils [(11,2),(22,2),(33,2)] -> cumsizes [0,2,4,6]. Seek to offset 2 should seek to 22.
        assert offset >= 0
        i = bisect.bisect_left( self.cumsizes, offset + 1 ) - 1
        assert i >= 0
        return i

    def close(self):
        self.fileobj.close()

    def closed(self):
        return self.fileobj.closed()

    def fileno(self):
        return self.fileobj.fileno()

    def seekable(self):
        return self.fileobj.seekable()

    def readable(self):
        return self.fileobj.readable()

    def writable(self):
        return False

    def read(self, size=-1):
        if size == -1:
            size = self.cumsizes[-1] - self.offset

        # This loop works in a kind of leapfrog fashion. On each even loop iteration it seeks to the next stencil
        # and on each odd iteration it reads the data and increments the offset inside the stencil!
        result = b''
        i = self._findStencil( self.offset )
        while size > 0 and i < len( self.sizes ):
            # Read as much as requested or as much as the current contiguous region / stencil still contains
            readableSize = min( size, self.sizes[i] - ( self.offset - self.cumsizes[i] ) )
            if readableSize == 0:
                # Go to next stencil
                i += 1
                if i >= len( self.offsets ):
                    break
                self.fileobj.seek( self.offsets[i] )
            else:
                # Actually read data
                tmp = self.fileobj.read( readableSize )
                self.offset += len( tmp )
                result += tmp
                size -= readableSize
                # Now, either size is 0 or readableSize will be 0 in the next iteration

        return result

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_CUR:
            self.offset += offset
        elif whence == io.SEEK_END:
            self.offset = self.cumsizes[-1] + offset
        elif whence == io.SEEK_SET:
            self.offset = offset

        if self.offset < 0:
            raise Exception("Trying to seek before the start of the file!")
        if self.offset >= self.cumsizes[-1]:
            return self.offset

        i = self._findStencil( self.offset )
        offsetInsideStencil = self.offset - self.cumsizes[i]
        assert offsetInsideStencil >= 0
        assert offsetInsideStencil < self.sizes[i]
        self.fileobj.seek( self.offsets[i] + offsetInsideStencil, io.SEEK_SET )

        return self.offset

    def tell(self):
        return self.offset


class SQLiteIndexedTar:
    """
    This class reads once through the whole TAR archive and stores TAR file offsets
    for all contained files in an index to support fast seeking to a given file.
    """

    __slots__ = (
        '__version__',
        'tarFileName',
        'mountRecursively',
        'indexFileName',
        'sqlConnection',
        'parentFolderCache', # stores which parent folders were last tried to add to database and therefore do exist
        'rawFileObject', # only set when opening a compressed file and only kept to keep the compressed file handle
                         # from being closed by the garbage collector
        'tarFileObject', # file object to the uncompressed (or decompressed) TAR file to read actual data out of
        'compression',   # stores what kind of compression the originally specified TAR file uses.
    )

    # Names must be identical to the SQLite column headers!
    FileInfo = collections.namedtuple( "FileInfo", "offsetheader offset size mtime mode type linkname uid gid istar issparse" )

    def __init__(
        self,
        tarFileName     = None,
        fileObject      = None,
        writeIndex      = False,
        clearIndexCache = False,
        recursive       = False,
        gzipSeekPointSpacing = 4*1024*1024,
    ):
        """
        tarFileName : Path to the TAR file to be opened. If not specified, a fileObject must be specified.
                      If only a fileObject is given, the created index can't be cached (efficiently).
        fileObject : A io.IOBase derived object. If not specified, tarFileName will be opened.
                     If it is an instance of IndexedBzip2File or IndexedGzipFile, then the offset
                     loading and storing from and to the SQLite database is managed automatically by this class.
        """
        # Version 0.1.0:
        #   - Initial version
        # Version 0.2.0:
        #   - Add sparse support and 'offsetheader' and 'issparse' columns to the SQLite database
        #   - Add TAR file size metadata in order to quickly check whether the TAR changed
        self.__version__ = '0.2.0'
        self.parentFolderCache = []
        self.mountRecursively = recursive
        self.sqlConnection = None

        assert tarFileName or fileObject
        if not tarFileName:
            self.tarFileName = '<file object>'
            self.createIndex( fileObject )
            # return here because we can't find a save location without any identifying name
            return

        self.tarFileName = os.path.abspath( tarFileName )
        if not fileObject:
            fileObject = open( self.tarFileName, 'rb' )
        self.tarFileObject, self.rawFileObject, self.compression = \
            SQLiteIndexedTar._openCompressedFile( fileObject, gzipSeekPointSpacing )

        # will be used for storing indexes if current path is read-only
        possibleIndexFilePaths = [
            self.tarFileName + ".index.sqlite",
            os.path.expanduser( os.path.join( "~", ".ratarmount",
                                              self.tarFileName.replace( "/", "_" ) + ".index.sqlite" ) )
        ]

        self.indexFileName = None
        if clearIndexCache:
            for indexPath in possibleIndexFilePaths:
                if os.path.isfile( indexPath ):
                    os.remove( indexPath )

        # Try to find an already existing index
        for indexPath in possibleIndexFilePaths:
            if self._tryLoadIndex( indexPath ):
                self.indexFileName = indexPath
                break
        if self.indexIsLoaded():
            self._loadOrStoreCompressionOffsets()
            return

        # Find a suitable (writable) location for the index database
        if writeIndex:
            for indexPath in possibleIndexFilePaths:
                try:
                    folder = os.path.dirname( indexPath )
                    os.makedirs( folder, exist_ok = True )

                    f = open( indexPath, 'wb' )
                    f.write( b'\0' * 1024 * 1024 )
                    f.close()
                    os.remove( indexPath )

                    self.indexFileName = indexPath
                    break
                except IOError:
                    if printDebug >= 2:
                        print( "Could not create file:", indexPath )

        self.createIndex( self.tarFileObject )
        self._loadOrStoreCompressionOffsets()

        self._storeTarMetadata()

        if printDebug >= 1 and writeIndex:
            # The 0-time is legacy for the automated tests
            print( "Writing out TAR index to", self.indexFileName, "took 0s",
                   "and is sized", os.stat( self.indexFileName ).st_size, "B" )

    def _storeTarMetadata( self ):
        """Adds some consistency meta information to recognize the need to update the cached TAR index"""

        metadataTable = """
            /* empty table whose sole existence specifies that we finished iterating the tar */
            CREATE TABLE "metadata" (
                "key"      VARCHAR(65535) NOT NULL, /* e.g. "tarsize" */
                "value"    VARCHAR(65535) NOT NULL  /* e.g. size in bytes as integer */
            );
        """

        try:
            tarStats = os.stat( self.tarFileName )
            self.sqlConnection.executescript( metadataTable )
            serializedTarStats = json.dumps( { attr : getattr( tarStats, attr )
                                               for attr in dir( tarStats ) if attr.startswith( 'st_' ) } )
            self.sqlConnection.execute( 'INSERT INTO "metadata" VALUES (?,?)', ( "tarstats", serializedTarStats ) )
            self.sqlConnection.commit()
        except Exception as exception:
            if printDebug >= 2:
                print( exception )
            print( "[Warning] There was an error when adding file metadata information." )
            print( "[Warning] Automatic detection of changed TAR files during index loading might not work." )

    def _openSqlDb( self, filePath ):
        self.sqlConnection = sqlite3.connect( filePath )
        self.sqlConnection.row_factory = sqlite3.Row
        self.sqlConnection.executescript( """
            PRAGMA LOCKING_MODE = EXCLUSIVE;
            PRAGMA TEMP_STORE = MEMORY;
            PRAGMA JOURNAL_MODE = OFF;
            PRAGMA SYNCHRONOUS = OFF;
        """ )

    def createIndex( self, fileObject, progressBar = None, pathPrefix = '', streamOffset = 0 ):
        if printDebug >= 1:
            print( "Creating offset dictionary for",
                   "<file object>" if self.tarFileName is None else self.tarFileName, "..." )
        t0 = timer()

        # 1. If no SQL connection was given (by recursive call), open a new database file
        openedConnection = False
        if not self.indexIsLoaded():
            if printDebug >= 1:
                print( "Creating new SQLite index database at", self.indexFileName )

            createTables = """
                CREATE TABLE "files" (
                    "path"          VARCHAR(65535) NOT NULL,
                    "name"          VARCHAR(65535) NOT NULL,
                    "offsetheader"  INTEGER,  /* seek offset from TAR file where these file's contents resides */
                    "offset"        INTEGER,  /* seek offset from TAR file where these file's contents resides */
                    "size"          INTEGER,
                    "mtime"         INTEGER,
                    "mode"          INTEGER,
                    "type"          INTEGER,
                    "linkname"      VARCHAR(65535),
                    "uid"           INTEGER,
                    "gid"           INTEGER,
                    /* True for valid TAR files. Internally used to determine where to mount recursive TAR files. */
                    "istar"         BOOL   ,
                    "issparse"      BOOL   ,  /* for sparse files the file size refers to the expanded size! */
                    PRIMARY KEY (path,name) /* see SQL benchmarks for decision on this */
                );
                /* "A table created using CREATE TABLE AS has no PRIMARY KEY and no constraints of any kind"
                 * Therefore, it will not be sorted and inserting will be faster! */
                CREATE TABLE "filestmp" AS SELECT * FROM "files" WHERE 0;
                CREATE TABLE "parentfolders" (
                    "path"     VARCHAR(65535) NOT NULL,
                    "name"     VARCHAR(65535) NOT NULL,
                    PRIMARY KEY (path,name)
                );
            """

            openedConnection = True
            self._openSqlDb( self.indexFileName if self.indexFileName else ':memory:' )
            tables = self.sqlConnection.execute( 'SELECT name FROM sqlite_master WHERE type = "table";' )
            if set( [ "files", "filestmp", "parentfolders" ] ).intersection( set( [ t[0] for t in tables ] ) ):
                raise Exception( "[Error] The index file {} already seems to contain a table. "
                                 "Please specify --recreate-index." )
            self.sqlConnection.executescript( createTables )

        # 2. Open TAR file reader
        try:
            streamed = ( hasBzip2Support and isinstance( fileObject, IndexedBzip2File ) ) or \
                       ( hasGzipSupport and isinstance( fileObject, IndexedGzipFile ) )
            # r: uses seeks to skip to the next file inside the TAR while r| doesn't do any seeks.
            # r| might be slower but for compressed files we have to go over all the data once anyways
            # and I had problems with seeks at this stage. Maybe they are gone now after the bz2 bugfix though.
            loadedTarFile = tarfile.open( fileobj = fileObject, mode = 'r|' if streamed else 'r:' )
        except tarfile.ReadError as exception:
            print( "Archive can't be opened! This might happen for compressed TAR archives, "
                   "which currently is not supported." )
            raise exception

        if progressBar is None:
            progressBar = ProgressBar( os.fstat( fileObject.fileno() ).st_size )

        # 3. Iterate over files inside TAR and add them to the database
        try:
          for tarInfo in loadedTarFile:
            loadedTarFile.members = []
            globalOffset = streamOffset + tarInfo.offset_data
            globalOffsetHeader = streamOffset + tarInfo.offset
            if hasBzip2Support and isinstance( fileObject, IndexedBzip2File ):
                # We will have to adjust the global offset to a rough estimate of the real compressed size.
                # Note that tell_compressed is always one bzip2 block further, which leads to underestimated
                # file compression ratio especially in the beginning.
                progressBar.update( int( globalOffset * fileObject.tell_compressed() / 8 / fileObject.tell() ) )
            elif hasGzipSupport and isinstance( fileObject, IndexedGzipFile ):
                try:
                    progressBar.update( int( globalOffset * fileObject.fileobj().tell() / fileObject.tell() ) )
                except:
                    progressBar.update( globalOffset )
            else:
                progressBar.update( globalOffset )

            mode = tarInfo.mode
            if tarInfo.isdir() : mode |= stat.S_IFDIR
            if tarInfo.isfile(): mode |= stat.S_IFREG
            if tarInfo.issym() : mode |= stat.S_IFLNK
            if tarInfo.ischr() : mode |= stat.S_IFCHR
            if tarInfo.isfifo(): mode |= stat.S_IFIFO

            # Add a leading '/' as a convention where '/' represents the TAR root folder
            # Partly, done because fusepy specifies paths in a mounted directory like this
            # os.normpath does not delete duplicate '/' at beginning of string!
            fullPath = pathPrefix + "/" + os.path.normpath( tarInfo.name ).lstrip( '/' )

            # 4. Open contained TARs for recursive mounting
            isTar = False
            if self.mountRecursively and tarInfo.isfile() and tarInfo.name.endswith( ".tar" ):
                oldPos = fileObject.tell()
                fileObject.seek( globalOffset )

                oldPrintName = self.tarFileName
                try:
                    self.tarFileName = tarInfo.name.lstrip( '/' ) # This is for output of the recursive call
                    self.createIndex( fileObject, progressBar, fullPath, globalOffset if streamed else 0 )

                    # if the TAR file contents could be read, we need to adjust the actual
                    # TAR file's metadata to be a directory instead of a file
                    mode = ( mode & 0o777 ) | stat.S_IFDIR
                    if mode & stat.S_IRUSR != 0: mode |= stat.S_IXUSR
                    if mode & stat.S_IRGRP != 0: mode |= stat.S_IXGRP
                    if mode & stat.S_IROTH != 0: mode |= stat.S_IXOTH
                    isTar = True

                except tarfile.ReadError:
                    None
                self.tarFileName = oldPrintName

                fileObject.seek( oldPos )

            path, name = fullPath.rsplit( "/", 1 )
            fileInfo = (
                path               , # 0
                name               , # 1
                globalOffsetHeader , # 2
                globalOffset       , # 3
                tarInfo.size       , # 4
                tarInfo.mtime      , # 5
                mode               , # 6
                tarInfo.type       , # 7
                tarInfo.linkname   , # 8
                tarInfo.uid        , # 9
                tarInfo.gid        , # 10
                isTar              , # 11
                tarInfo.issparse() , # 12
            )
            self._setFileInfo( fileInfo )
        except tarfile.ReadError as e:
            if 'unexpected end of data' in str( e ):
                print( "[Warning] The TAR file is incomplete. Ratarmount will work but some files might be cut off. "
                       "If the TAR file size changes, ratarmount will recreate the index during the next mounting." )

        # 5. Resort by (path,name). This one-time resort is faster than resorting on each INSERT (cache spill)
        if openedConnection:
            if printDebug >= 2:
                print( "Resorting files by path ..." )

            cleanupDatabase = """
                INSERT OR REPLACE INTO "files" SELECT * FROM "filestmp" ORDER BY "path","name",rowid;
                DROP TABLE "filestmp";
                INSERT OR IGNORE INTO "files"
                    /* path name offsetheader offset size mtime mode type linkname uid gid istar issparse */
                    SELECT path,name,0,0,1,0,{},{},"",0,0,0,0
                    FROM "parentfolders" ORDER BY "path","name";
                DROP TABLE "parentfolders";
            """.format( int( 0o555 | stat.S_IFDIR ), int( tarfile.DIRTYPE ) )
            self.sqlConnection.executescript( cleanupDatabase )

        # 6. Add Metadata
        metadataTables = """
            /* empty table whose sole existence specifies that we finished iterating the tar */
            CREATE TABLE "versions" (
                "name"     VARCHAR(65535) NOT NULL, /* which component the version belongs to */
                "version"  VARCHAR(65535) NOT NULL, /* free form version string */
                /* Semantic Versioning 2.0.0 (semver.org) parts if they can be specified:
                 *   MAJOR version when you make incompatible API changes,
                 *   MINOR version when you add functionality in a backwards compatible manner, and
                 *   PATCH version when you make backwards compatible bug fixes. */
                "major"    INTEGER,
                "minor"    INTEGER,
                "patch"    INTEGER
            );
        """
        try:
            self.sqlConnection.executescript( metadataTables )
        except Exception as exception:
            if printDebug >= 2:
                print( exception )
            print( "[Warning] There was an error when adding metadata information. Index loading might not work." )

        try:
            def makeVersionRow( versionName, version ):
                versionNumbers = [ re.sub( '[^0-9]', '', x ) for x in version.split( '.' ) ]
                return ( versionName,
                         version,
                         versionNumbers[0] if len( versionNumbers ) > 0 else None,
                         versionNumbers[1] if len( versionNumbers ) > 1 else None,
                         versionNumbers[2] if len( versionNumbers ) > 2 else None, )

            versions = [ makeVersionRow( 'ratarmount', __version__ ),
                         makeVersionRow( 'index', self.__version__ ) ]

            if hasBzip2Support and isinstance( fileObject, IndexedBzip2File ):
                versions += [ makeVersionRow( 'indexed_bzip2', indexed_bzip2.__version__ ) ]

            if hasGzipSupport and isinstance( fileObject, IndexedGzipFile ):
                versions += [ makeVersionRow( 'indexed_gzip', indexed_gzip.__version__ ) ]

            self.sqlConnection.executemany( 'INSERT OR REPLACE INTO "versions" VALUES (?,?,?,?,?)', versions )
        except Exception as exception:
            print( "[Warning] There was an error when adding version information." )
            if printDebug >= 3:
                print( exception )

        self.sqlConnection.commit()

        t1 = timer()
        if printDebug >= 1:
            print( "Creating offset dictionary for",
                   "<file object>" if self.tarFileName is None else self.tarFileName,
                   "took {:.2f}s".format( t1 - t0 ) )

    def getFileInfo( self, fullPath, listDir = False ):
        """
        This is the heart of this class' public interface!

        path    : full path to file where '/' denotes TAR's root, e.g., '/', or '/foo'
        listDir : if True, return a dictionary for the given directory path: { fileName : FileInfo, ... }
                  if False, return simple FileInfo to given path (directory or file)
        if path does not exist, always return None
        """
        # @todo cache last listDir as most ofthen a stat over all entries will soon follow

        # also strips trailing '/' except for a single '/' and leading '/'
        fullPath = '/' + os.path.normpath( fullPath ).lstrip( '/' )
        if listDir:
            rows = self.sqlConnection.execute( 'SELECT * FROM "files" WHERE "path" == (?)',
                                               ( fullPath.rstrip( '/' ), ) )
            dir = {}
            gotResults = False
            for row in rows:
                gotResults = True
                if row['name']:
                    dir[row['name']] = self.FileInfo(
                        offset       = row['offset'],
                        offsetheader = row['offsetheader'] if 'offsetheader' in row.keys() else 0,
                        size         = row['size'],
                        mtime        = row['mtime'],
                        mode         = row['mode'],
                        type         = row['type'],
                        linkname     = row['linkname'],
                        uid          = row['uid'],
                        gid          = row['gid'],
                        istar        = row['istar'],
                        issparse     = row['issparse'] if 'issparse' in row.keys() else False
                    )

            return dir if gotResults else None

        path, name = fullPath.rsplit( '/', 1 )
        row = self.sqlConnection.execute( 'SELECT * FROM "files" WHERE "path" == (?) AND "name" == (?)',
                                          ( path, name ) ).fetchone()

        if row is None:
            return None

        return self.FileInfo(
            offset       = row['offset'],
            offsetheader = row['offsetheader'] if 'offsetheader' in row.keys() else 0,
            size         = row['size'],
            mtime        = row['mtime'],
            mode         = row['mode'],
            type         = row['type'],
            linkname     = row['linkname'],
            uid          = row['uid'],
            gid          = row['gid'],
            istar        = row['istar'],
            issparse     = row['issparse'] if 'issparse' in row.keys() else False
        )

    def isDir( self, path ):
        return isinstance( self.getFileInfo( path, listDir = True ), dict )

    def _tryAddParentFolders( self, path ):
        # Add parent folders if they do not exist.
        # E.g.: path = '/a/b/c' -> paths = [('', 'a'), ('/a', 'b'), ('/a/b', 'c')]
        # Without the parentFolderCache, the additional INSERT statements increase the creation time
        # from 8.5s to 12s, so almost 50% slowdown for the 8MiB test TAR!
        paths = path.split( "/" )
        paths = [ p for p in ( ( "/".join( paths[:i] ), paths[i] ) for i in range( 1, len( paths ) ) )
                 if p not in self.parentFolderCache ]
        if not paths:
            return

        self.parentFolderCache += paths
        # Assuming files in the TAR are sorted by hierarchy, the maximum parent folder cache size
        # gives the maximum cacheable file nesting depth. High numbers lead to higher memory usage and lookup times.
        if len( self.parentFolderCache ) > 16:
            self.parentFolderCache = self.parentFolderCache[-8:]
        self.sqlConnection.executemany( 'INSERT OR IGNORE INTO "parentfolders" VALUES (?,?)',
                                        [ ( p[0], p[1] ) for p in paths ] )

    def _setFileInfo( self, row ):
        assert isinstance( row, tuple )
        self.sqlConnection.execute( 'INSERT OR REPLACE INTO "files" VALUES (' +
                                    ','.join( '?' * len( row ) ) + ');', row )
        self._tryAddParentFolders( row[0] )

    def setFileInfo( self, fullPath, fileInfo ):
        """
        fullPath : the full path to the file with leading slash (/) for which to set the file info
        """
        assert self.sqlConnection
        assert fullPath[0] == "/"
        assert isinstance( fileInfo, self.FileInfo )

        # os.normpath does not delete duplicate '/' at beginning of string!
        path, name = fullPath.rsplit( "/", 1 )
        row = (
            path,
            name,
            fileInfo.offsetheader,
            fileInfo.offset,
            fileInfo.size,
            fileInfo.mtime,
            fileInfo.mode,
            fileInfo.type,
            fileInfo.linkname,
            fileInfo.uid,
            fileInfo.gid,
            fileInfo.istar,
            fileInfo.issparse,
        )
        self._setFileInfo( row )

    def indexIsLoaded( self ):
        if not self.sqlConnection:
            return False

        try:
            self.sqlConnection.execute( 'SELECT * FROM "files" WHERE 0 == 1;' )
        except sqlite3.OperationalError:
            self.sqlConnection = None
            return False

        return True

    def loadIndex( self, indexFileName ):
        """Loads the given index SQLite database and checks it for validity."""
        if self.indexIsLoaded():
            return

        t0 = time.time()
        self._openSqlDb( indexFileName )
        tables = [ x[0] for x in self.sqlConnection.execute( 'SELECT name FROM sqlite_master WHERE type="table"' ) ]
        versions = None
        try:
            rows = self.sqlConnection.execute( 'SELECT * FROM versions;' )
            versions = {}
            for row in rows:
                versions[row[0]] = ( row[2], row[3], row[4] )
        except:
            pass

        try:
            # Check indexes created with bugged bz2 decoder (bug existed when I did not store versions yet)
            if 'bzip2blocks' in tables and 'versions' not in tables:
                raise Exception( "The indexes created with version 0.3.0 through 0.3.3 for bzip2 compressed archives "
                                 "are very likely to be wrong because of a bzip2 decoder bug.\n"
                                 "Please delete the index or call ratarmount with the --recreate-index option!" )

            # Check for empty or incomplete indexes
            if 'files' not in tables:
                raise Exception( "SQLite index is empty" )

            if 'filestmp' in tables or 'parentfolders' in tables:
                raise Exception( "SQLite index is incomplete" )

            # Check for pre-sparse support indexes
            if 'versions' not in tables or 'index' not in versions or versions['index'][1] < 2:
                print( "[Warning] The found outdated index does not contain any sparse file information." )
                print( "[Warning] Please recreate the index if you have problems with those." )

            if 'metadata' in tables:
                values = dict( list( self.sqlConnection.execute( 'SELECT * FROM metadata;' ) ) )
                if 'tarstats' in values:
                    values = json.loads( values['tarstats'] )
                tarStats = os.stat( self.tarFileName )

                if hasattr( tarStats, "st_size" ) and 'st_size' in values \
                   and tarStats.st_size != values['st_size']:
                    raise Exception( "TAR file for this SQLite index has changed size from",
                                     tarStats.st_size, "to", values['st_size'] )

                if hasattr( tarStats, "st_mtime" ) and 'st_mtime' in values \
                   and tarStats.st_mtime != values['st_mtime']:
                    raise Exception( "The modification date for the TAR file", values['st_mtime'],
                                     "to this SQLite index has changed (" + str( tarStats.st_mtime ) + ")" )

        except Exception as e:
            # indexIsLoaded checks self.sqlConnection, so close it before returning because it was found to be faulty
            try:
                self.sqlConnection.close()
            except:
                pass
            self.sqlConnection = None

            raise e

        if printDebug >= 1:
            # Legacy output for automated tests
            print( "Loading offset dictionary from", indexFileName, "took {:.2f}s".format( time.time() - t0 ) )

    def _tryLoadIndex( self, indexFileName ):
        """calls loadIndex if index is not loaded already and provides extensive error handling"""

        if self.indexIsLoaded():
            return True

        if not os.path.isfile( indexFileName ):
            return False

        try:
            self.loadIndex( indexFileName )
        except Exception as exception:
            if printDebug >= 3:
                traceback.print_exc()

            print( "[Warning] Could not load file '" + indexFileName  )
            print( "[Info] Exception:", exception )
            print( "[Info] Some likely reasons for not being able to load the index file:" )
            print( "[Info]   - The index file has incorrect read permissions" )
            print( "[Info]   - The index file is incomplete because ratarmount was killed during index creation" )
            print( "[Info]   - The index file was detected to contain errors because of known bugs of older versions" )
            print( "[Info]   - The index file got corrupted because of:" )
            print( "[Info]     - The program exited while it was still writing the index because of:" )
            print( "[Info]       - the user sent SIGINT to force the program to quit" )
            print( "[Info]       - an internal error occured while writing the index" )
            print( "[Info]       - the disk filled up while writing the index" )
            print( "[Info]     - Rare lowlevel corruptions caused by hardware failure" )

            print( "[Info] This might force a time-costly index recreation, so if it happens often\n"
                   "       and mounting is slow, try to find out why loading fails repeatedly,\n"
                   "       e.g., by opening an issue on the public github page." )

            try:
                os.remove( indexFileName )
            except OSError:
                print( "[Warning] Failed to remove corrupted old cached index file:", indexFileName )

        if printDebug >= 3 and self.indexIsLoaded():
            print( "Loaded index", indexFileName )

        return self.indexIsLoaded()

    @staticmethod
    def _detectCompression( name = None, fileobj = None ):
        oldOffset = None
        if fileobj:
            assert fileobj.seekable()
            oldOffset = fileobj.tell()
            if name is None:
                name = fileobj.name

        for compression in [ '', 'bz2', 'gz', 'xz' ]:
            try:
                # Simply opening a TAR file should be fast as only the header should be read!
                tarfile.open( name = name, fileobj = fileobj, mode = 'r:' + compression )

                if compression == 'bz2' and 'IndexedBzip2File' not in globals():
                    raise Exception( "Can't open a bzip2 compressed TAR file '{}' without indexed_bzip2 module!"
                                     .format( name ) )
                elif compression == 'gz' and 'IndexedGzipFile' not in globals():
                    raise Exception( "Can't open a bzip2 compressed TAR file '{}' without indexed_gzip module!"
                                     .format( name ) )
                elif compression == 'xz':
                    raise Exception( "Can't open xz compressed TAR file '{}'!".format( name ) )

                if oldOffset is not None:
                    fileobj.seek( oldOffset )
                return compression

            except tarfile.ReadError as e:
                if oldOffset is not None:
                    fileobj.seek( oldOffset )
                pass

        raise Exception( "File '{}' does not seem to be a valid TAR file!".format( name ) )

    @staticmethod
    def _openCompressedFile( fileobj, gzipSeekPointSpacing ):
        """Opens a file possibly undoing the compression."""
        rawFile = None
        tarFile = fileobj
        compression = SQLiteIndexedTar._detectCompression( fileobj = tarFile )

        if compression == 'bz2':
            rawFile = tarFile # save so that garbage collector won't close it!
            tarFile = IndexedBzip2File( rawFile.fileno() )
        elif compression == 'gz':
            rawFile = tarFile # save so that garbage collector won't close it!
            # drop_handles keeps a file handle opening as is required to call tell() during decoding
            tarFile = IndexedGzipFile( fileobj = rawFile,
                                       drop_handles = False,
                                       spacing = gzipSeekPointSpacing )

        return tarFile, rawFile, compression

    def _loadOrStoreCompressionOffsets( self ):
        # This should be called after the TAR file index is complete (loaded or created).
        # If the TAR file index was created, then tarfile has iterated over the whole file once
        # and therefore completed the implicit compression offset creation.
        db = self.sqlConnection
        fileObject = self.tarFileObject

        if 'IndexedBzip2File' in globals() and isinstance( fileObject, IndexedBzip2File ):
            try:
                offsets = dict( db.execute( 'SELECT blockoffset,dataoffset FROM bzip2blocks;' ) )
                fileObject.set_block_offsets( offsets )
            except Exception as e:
                if printDebug >= 2:
                    print( "[Info] Could not load BZip2 Block offset data. Will create it from scratch." )

                tables = [ x[0] for x in db.execute( 'SELECT name FROM sqlite_master WHERE type="table";' ) ]
                if 'bzip2blocks' in tables:
                    db.execute( 'DROP TABLE bzip2blocks' )
                db.execute( 'CREATE TABLE bzip2blocks ( blockoffset INTEGER PRIMARY KEY, dataoffset INTEGER )' )
                db.executemany( 'INSERT INTO bzip2blocks VALUES (?,?)',
                                fileObject.block_offsets().items() )
                db.commit()
            return

        if 'IndexedGzipFile' in globals() and isinstance( fileObject, IndexedGzipFile ):
            # indexed_gzip index only has a file based API, so we need to write all the index data from the SQL
            # database out into a temporary file. For that, let's first try to use the same location as the SQLite
            # database because it should have sufficient writing rights and free disk space.
            gzindex = None
            for tmpDir in [ os.path.dirname( self.indexFileName ), None ]:
                # Try to export data from SQLite database. Note that no error checking against the existence of
                # gzipindex table is done because the exported data itself might also be wrong and we can't check
                # against this. Therefore, collate all error checking by catching exceptions.
                try:
                    gzindex = tempfile.mkstemp( dir = tmpDir )[1]
                    with open( gzindex, 'wb' ) as file:
                        file.write( db.execute( 'SELECT data FROM gzipindex' ).fetchone()[0] )
                except:
                    try:
                        os.remove( gzindex )
                    except:
                        pass
                    gzindex = None

            try:
                fileObject.import_index( filename = gzindex )
                return
            except:
                pass

            try:
                os.remove( gzindex )
            except:
                pass

            # Store the offsets into a temporary file and then into the SQLite database
            if printDebug >= 2:
                print( "[Info] Could not load GZip Block offset data. Will create it from scratch." )

            # Transparently force index to be built if not already done so. build_full_index was buggy for me.
            # Seeking from end not supported, so we have to read the whole data in in a loop
            while fileObject.read( 1024*1024 ):
                pass

            # The created index can unfortunately be pretty large and tmp might actually run out of memory!
            # Therefore, try different paths, starting with the location where the index resides.
            gzindex = None
            for tmpDir in [ os.path.dirname( self.indexFileName ), None ]:
                gzindex = tempfile.mkstemp( dir = tmpDir )[1]
                try:
                    fileObject.export_index( filename = gzindex )
                except indexed_gzip.ZranError:
                    try:
                        os.remove( gzindex )
                    except:
                        pass
                    gzindex = None

            if not gzindex or not os.path.isfile( gzindex ):
                print( "[Warning] The GZip index required for seeking could not be stored in a temporary file!" )
                print( "[Info] This might happen when you are out of space in your temporary file and at the" )
                print( "[Info] the index file location. The gzipindex size takes roughly 32kiB per 4MiB of" )
                print( "[Info] uncompressed(!) bytes (0.8% of the uncompressed data) by default." )
                raise Exception( "[Error] Could not initialize the GZip seek cache." )
            elif printDebug >= 2:
                print( "Exported GZip index size:", os.stat( gzindex ).st_size )

            # Store contents of temporary file into the SQLite database
            tables = [ x[0] for x in db.execute( 'SELECT name FROM sqlite_master WHERE type="table"' ) ]
            if 'gzipindex' in tables:
                db.execute( 'DROP TABLE gzipindex' )
            db.execute( 'CREATE TABLE gzipindex ( data BLOB )' )
            with open( gzindex, 'rb' ) as file:
                db.execute( 'INSERT INTO gzipindex VALUES (?)', ( file.read(), ) )
            db.commit()
            os.remove( gzindex )

class IndexedTar:
    """
    This class reads once through the whole TAR archive and stores TAR file offsets
    for all contained files in an index to support fast seeking to a given file.
    """

    __slots__ = (
        'tarFileName',
        'fileIndex',
        'mountRecursively',
        'cacheFolder',
        'possibleIndexFilePaths',
        'indexFileName',
        'progressBar',
        'tarFileObject',
    )

    FileInfo = collections.namedtuple( "FileInfo", "offset size mtime mode type linkname uid gid istar" )

    # these allowed backends also double as extensions for the index file to look for
    availableSerializationBackends = [
        'none',
        'pickle',
        'pickle2',
        'pickle3',
        'custom',
        'cbor',
        'msgpack',
        'rapidjson',
        'ujson',
        'simplejson'
    ]
    availableCompressions = [
        '', # no compression
        'lz4',
        'gz',
    ]

    def __init__( self,
                  pathToTar = None,
                  fileObject = None,
                  writeIndex = False,
                  clearIndexCache = False,
                  recursive = False,
                  serializationBackend = None,
                  progressBar = None ):
        self.progressBar = progressBar
        self.tarFileName = os.path.normpath( pathToTar )

        # Stores the file hierarchy in a dictionary with keys being either
        #  - the file and containing file metainformation
        #  - or keys being a folder name and containing a recursively defined dictionary.
        self.fileIndex = {}
        self.mountRecursively = recursive

        # will be used for storing indexes if current path is read-only
        self.cacheFolder = os.path.expanduser( "~/.ratarmount" )
        self.possibleIndexFilePaths = [
            self.tarFileName + ".index",
            self.cacheFolder + "/" + self.tarFileName.replace( "/", "_" ) + ".index"
        ]

        if not serializationBackend:
            serializationBackend = 'custom'

        if serializationBackend not in self.supportedIndexExtensions():
            print( "[Warning] Serialization backend '" + str( serializationBackend ) + "' not supported.",
                   "Defaulting to '" + serializationBackend + "'!" )
            print( "List of supported extensions / backends:", self.supportedIndexExtensions() )

            serializationBackend = 'custom'

        # this is the actual index file, which will be used in the end, and by default
        self.indexFileName = self.possibleIndexFilePaths[0] + "." + serializationBackend

        if clearIndexCache:
            for indexPath in self.possibleIndexFilePaths:
                for extension in self.supportedIndexExtensions():
                    indexPathWitExt = indexPath + "." + extension
                    if os.path.isfile( indexPathWitExt ):
                        os.remove( indexPathWitExt )

        if fileObject is not None:
            if writeIndex:
                print( "Can't write out index for file object input. Ignoring this option." )
            self.createIndex( fileObject )
        else:
            fileObject = open( self.tarFileName, 'rb' )

            # first try loading the index for the given serialization backend
            if serializationBackend is not None:
                for indexPath in self.possibleIndexFilePaths:
                    if self.tryLoadIndex( indexPath + "." + serializationBackend ):
                        break

            # try loading the index from one of the pre-configured paths
            for indexPath in self.possibleIndexFilePaths:
                for extension in self.supportedIndexExtensions():
                    if self.tryLoadIndex( indexPath + "." + extension ):
                        break

            if not self.indexIsLoaded():
                self.createIndex( fileObject )

                if writeIndex:
                    for indexPath in self.possibleIndexFilePaths:
                        indexPath += "." + serializationBackend

                        try:
                            folder = os.path.dirname( indexPath )
                            if not os.path.exists( folder ):
                                os.mkdir( folder )

                            f = open( indexPath, 'wb' )
                            f.close()
                            os.remove( indexPath )
                            self.indexFileName = indexPath

                            break
                        except IOError:
                            if printDebug >= 2:
                                print( "Could not create file:", indexPath )

                    try:
                        self.writeIndex( self.indexFileName )
                    except IOError:
                        print( "[Info] Could not write TAR index to file. ",
                               "Subsequent mounts might be slow!" )

        self.tarFileObject = fileObject

    @staticmethod
    def supportedIndexExtensions():
        return [ '.'.join( combination ).strip( '.' )
                 for combination in itertools.product( IndexedTar.availableSerializationBackends,
                                                       IndexedTar.availableCompressions ) ]
    @staticmethod
    def dump( toDump, file ):
        import msgpack

        if isinstance( toDump, dict ):
            file.write( b'\x01' ) # magic code meaning "start dictionary object"

            for key, value in toDump.items():
                file.write( b'\x03' ) # magic code meaning "serialized key value pair"
                IndexedTar.dump( key, file )
                IndexedTar.dump( value, file )

            file.write( b'\x02' ) # magic code meaning "close dictionary object"

        elif isinstance( toDump, IndexedTar.FileInfo ):
            serialized = msgpack.dumps( toDump )
            file.write( b'\x05' ) # magic code meaning "msgpack object"
            file.write( len( serialized ).to_bytes( 4, byteorder = 'little' ) )
            file.write( serialized )

        elif isinstance( toDump, str ):
            serialized = toDump.encode()
            file.write( b'\x04' ) # magic code meaning "string object"
            file.write( len( serialized ).to_bytes( 4, byteorder = 'little' ) )
            file.write( serialized )

        else:
            print( "Ignoring unsupported type to write:", toDump )

    @staticmethod
    def load( file ):
        import msgpack

        elementType = file.read( 1 )

        if elementType != b'\x01': # start of dictionary
            raise Exception( 'Custom TAR index loader: invalid file format' )

        result = {}

        dictElementType = file.read( 1 )
        while dictElementType:
            if dictElementType == b'\x02':
                break

            elif dictElementType == b'\x03':
                keyType = file.read( 1 )
                if keyType != b'\x04': # key must be string object
                    raise Exception( 'Custom TAR index loader: invalid file format' )
                size = int.from_bytes( file.read( 4 ), byteorder = 'little' )
                key = file.read( size ).decode()

                valueType = file.read( 1 )
                if valueType == b'\x05': # msgpack object
                    size = int.from_bytes( file.read( 4 ), byteorder = 'little' )
                    serialized = file.read( size )
                    value = IndexedTar.FileInfo( *msgpack.loads( serialized ) )

                elif valueType == b'\x01': # dict object
                    file.seek( -1, io.SEEK_CUR )
                    value = IndexedTar.load( file )

                else:
                    raise Exception(
                        'Custom TAR index loader: invalid file format ' +
                        '(expected msgpack or dict but got' +
                        str( int.from_bytes( valueType, byteorder = 'little' ) ) + ')' )

                result[key] = value

            else:
                raise Exception(
                    'Custom TAR index loader: invalid file format ' +
                    '(expected end-of-dict or key-value pair but got' +
                    str( int.from_bytes( dictElementType, byteorder = 'little' ) ) + ')' )

            dictElementType = file.read( 1 )

        return result

    def getFileInfo( self, path, listDir = False ):
        # go down file hierarchy tree along the given path
        p = self.fileIndex
        for name in os.path.normpath( path ).split( os.sep ):
            if not name:
                continue
            if name not in p:
                return None
            p = p[name]

        def repackDeserializedNamedTuple( p ):
            if isinstance( p, list ) and len( p ) == len( self.FileInfo._fields ):
                return self.FileInfo( *p )

            if isinstance( p, dict ) and len( p ) == len( self.FileInfo._fields ) and \
                 'uid' in p and isinstance( p['uid'], int ):
                # a normal directory dict must only have dict or FileInfo values,
                # so if the value to the 'uid' key is an actual int,
                # then it is sure it is a deserialized FileInfo object and not a file named 'uid'
                print( "P ===", p )
                print( "FileInfo ===", self.FileInfo( **p ) )
                return self.FileInfo( **p )

            return p

        p = repackDeserializedNamedTuple( p )

        # if the directory contents are not to be printed and it is a directory,
        # return the "file" info of ".", which holds the directory metainformation
        if not listDir and isinstance( p, dict ):
            if '.' in p:
                p = p['.']
            else:
                return self.FileInfo(
                    offset   = 0, # not necessary for directory anyways
                    size     = 1, # might be misleading / non-conform
                    mtime    = 0,
                    mode     = 0o555 | stat.S_IFDIR,
                    type     = tarfile.DIRTYPE,
                    linkname = "",
                    uid      = 0,
                    gid      = 0,
                    istar    = False
                )

        return repackDeserializedNamedTuple( p )

    def isDir( self, path ):
        return isinstance( self.getFileInfo( path, listDir = True ), dict )

    def exists( self, path ):
        path = os.path.normpath( path )
        return self.isDir( path ) or isinstance( self.getFileInfo( path ), self.FileInfo )

    def setFileInfo( self, path, fileInfo ):
        """
        path: the full path to the file with leading slash (/) for which to set the file info
        """
        assert isinstance( fileInfo, self.FileInfo )

        pathHierarchy = os.path.normpath( path ).split( os.sep )
        if not pathHierarchy:
            return

        # go down file hierarchy tree along the given path
        p = self.fileIndex
        for name in pathHierarchy[:-1]:
            if not name:
                continue
            assert isinstance( p, dict )
            p = p.setdefault( name, {} ) # if parent folders of the file to add do not exist, add them

        # create a new key in the dictionary of the parent folder
        p.update( { pathHierarchy[-1] : fileInfo } )

    def setDirInfo( self, path, dirInfo, dirContents = {} ):
        """
        path: the full path to the file with leading slash (/) for which to set the folder info
        """
        assert isinstance( dirInfo, self.FileInfo )
        assert isinstance( dirContents, dict )

        pathHierarchy = os.path.normpath( path ).strip( os.sep ).split( os.sep )
        if not pathHierarchy:
            return

        # go down file hierarchy tree along the given path
        p = self.fileIndex
        for name in pathHierarchy[:-1]:
            if not name:
                continue
            assert isinstance( p, dict )
            p = p.setdefault( name, {} )

        # create a new key in the dictionary of the parent folder
        p.update( { pathHierarchy[-1] : dirContents } )
        p[pathHierarchy[-1]].update( { '.' : dirInfo } )

    def createIndex( self, fileObject ):
        if printDebug >= 1:
            print( "Creating offset dictionary for",
                   "<file object>" if self.tarFileName is None else self.tarFileName, "..." )
        t0 = timer()

        self.fileIndex = {}
        try:
            loadedTarFile = tarfile.open( fileobj = fileObject, mode = 'r:' )
        except tarfile.ReadError as exception:
            print( "Archive can't be opened! This might happen for compressed TAR archives, "
                   "which currently is not supported." )
            raise exception

        if self.progressBar is None and os.path.isfile( self.tarFileName ):
            self.progressBar = ProgressBar( os.stat( self.tarFileName ).st_size )

        for tarInfo in loadedTarFile:
            loadedTarFile.members = []
            if self.progressBar is not None:
                self.progressBar.update( tarInfo.offset_data )

            mode = tarInfo.mode
            if tarInfo.isdir() : mode |= stat.S_IFDIR
            if tarInfo.isfile(): mode |= stat.S_IFREG
            if tarInfo.issym() : mode |= stat.S_IFLNK
            if tarInfo.ischr() : mode |= stat.S_IFCHR
            if tarInfo.isfifo(): mode |= stat.S_IFIFO
            fileInfo = self.FileInfo(
                offset   = tarInfo.offset_data,
                size     = tarInfo.size       ,
                mtime    = tarInfo.mtime      ,
                mode     = mode               ,
                type     = tarInfo.type       ,
                linkname = tarInfo.linkname   ,
                uid      = tarInfo.uid        ,
                gid      = tarInfo.gid        ,
                istar    = False
            )

            # open contained tars for recursive mounting
            indexedTar = None
            if self.mountRecursively and tarInfo.isfile() and tarInfo.name.endswith( ".tar" ):
                oldPos = fileObject.tell()
                if oldPos != tarInfo.offset_data:
                    fileObject.seek( tarInfo.offset_data )
                indexedTar = IndexedTar( tarInfo.name,
                                         fileObject = fileObject,
                                         writeIndex = False,
                                         progressBar = self.progressBar )
                # might be especially necessary if the .tar is not actually a tar!
                fileObject.seek( fileObject.tell() )

            # Add a leading '/' as a convention where '/' represents the TAR root folder
            # Partly, done because fusepy specifies paths in a mounted directory like this
            path = os.path.normpath( "/" + tarInfo.name )

            # test whether the TAR file could be loaded and if so "mount" it recursively
            if indexedTar is not None and indexedTar.indexIsLoaded():
                # actually apply the recursive tar mounting
                mountMode = ( fileInfo.mode & 0o777 ) | stat.S_IFDIR
                if mountMode & stat.S_IRUSR != 0: mountMode |= stat.S_IXUSR
                if mountMode & stat.S_IRGRP != 0: mountMode |= stat.S_IXGRP
                if mountMode & stat.S_IROTH != 0: mountMode |= stat.S_IXOTH
                fileInfo = fileInfo._replace( mode = mountMode, istar = True )

                if self.exists( path ):
                    print( "[Warning]", path, "already exists in database and will be overwritten!" )

                # merge fileIndex from recursively loaded TAR into our Indexes
                self.setDirInfo( path, fileInfo, indexedTar.fileIndex )

            elif path != '/':
                # just a warning and check for the path already existing
                if self.exists( path ):
                    fileInfo = self.getFileInfo( path, listDir = False )
                    print( "[Warning]", path, "already exists in database and will be overwritten!" )

                # simply store the file or directory information from current TAR item
                if tarInfo.isdir():
                    self.setDirInfo( path, fileInfo, {} )
                else:
                    self.setFileInfo( path, fileInfo )

        t1 = timer()
        if printDebug >= 1:
            print( "Creating offset dictionary for",
                   "<file object>" if self.tarFileName is None else self.tarFileName,
                   "took {:.2f}s".format( t1 - t0 ) )

    def serializationBackendFromFileName( self, fileName ):
        splitName = fileName.split( '.' )

        if len( splitName ) > 2 and '.'.join( splitName[-2:] ) in self.supportedIndexExtensions():
            return '.'.join( splitName[-2:] )

        if splitName[-1] in self.supportedIndexExtensions():
            return splitName[-1]

        return None

    def indexIsLoaded( self ):
        return bool( self.fileIndex )

    def writeIndex( self, outFileName ):
        """
        outFileName: Full file name with backend extension.
                     Depending on the extension the serialization is chosen.
        """

        serializationBackend = self.serializationBackendFromFileName( outFileName )

        if printDebug >= 1:
            print( "Writing out TAR index using", serializationBackend, "to", outFileName, "..." )
        t0 = timer()

        fileMode = 'wt' if 'json' in serializationBackend else 'wb'

        if serializationBackend.endswith( '.lz4' ):
            import lz4.frame
            wrapperOpen = lambda x : lz4.frame.open( x, fileMode )
        elif serializationBackend.endswith( '.gz' ):
            import gzip
            wrapperOpen = lambda x : gzip.open( x, fileMode )
        else:
            wrapperOpen = lambda x : open( x, fileMode )
        serializationBackend = serializationBackend.split( '.' )[0]

        # libraries tested but not working:
        #  - marshal: can't serialize namedtuples
        #  - hickle: for some reason, creates files almost 64x larger and slower than pickle!?
        #  - yaml: almost a 10 times slower and more memory usage and deserializes everything including ints to string

        if serializationBackend == 'none':
            print( "Won't write out index file because backend 'none' was chosen. "
                   "Subsequent mounts might be slow!" )
            return

        with wrapperOpen( outFileName ) as outFile:
            if serializationBackend == 'pickle2':
                import pickle
                pickle.dump( self.fileIndex, outFile, protocol = 2 )

            # default serialization because it has the fewest dependencies and because it was legacy default
            elif serializationBackend == 'pickle3' or \
                 serializationBackend == 'pickle' or \
                 serializationBackend is None:
                import pickle
                pickle.dump( self.fileIndex, outFile, protocol = 3 ) # 3 is default protocol

            elif serializationBackend == 'simplejson':
                import simplejson
                simplejson.dump( self.fileIndex, outFile, namedtuple_as_object = True )

            elif serializationBackend == 'custom':
                IndexedTar.dump( self.fileIndex, outFile )

            elif serializationBackend in [ 'msgpack', 'cbor', 'rapidjson', 'ujson' ]:
                import importlib
                module = importlib.import_module( serializationBackend )
                getattr( module, 'dump' )( self.fileIndex, outFile )

            else:
                print( "Tried to save index with unsupported extension backend:", serializationBackend, "!" )

        t1 = timer()
        if printDebug >= 1:
            print( "Writing out TAR index to", outFileName, "took {:.2f}s".format( t1 - t0 ),
                   "and is sized", os.stat( outFileName ).st_size, "B" )

    def loadIndex( self, indexFileName ):
        if printDebug >= 1:
            print( "Loading offset dictionary from", indexFileName, "..." )
        t0 = timer()

        serializationBackend = self.serializationBackendFromFileName( indexFileName )

        fileMode = 'rt' if 'json' in serializationBackend else 'rb'

        if serializationBackend.endswith( '.lz4' ):
            import lz4.frame
            wrapperOpen = lambda x : lz4.frame.open( x, fileMode )
        elif serializationBackend.endswith( '.gz' ):
            import gzip
            wrapperOpen = lambda x : gzip.open( x, fileMode )
        else:
            wrapperOpen = lambda x : open( x, fileMode )
        serializationBackend = serializationBackend.split( '.' )[0]

        with wrapperOpen( indexFileName ) as indexFile:
            if serializationBackend in ( 'pickle2', 'pickle3', 'pickle' ):
                import pickle
                self.fileIndex = pickle.load( indexFile )

            elif serializationBackend == 'custom':
                self.fileIndex = IndexedTar.load( indexFile )

            elif serializationBackend == 'msgpack':
                import msgpack
                self.fileIndex = msgpack.load( indexFile, raw = False )

            elif serializationBackend == 'simplejson':
                import simplejson
                self.fileIndex = simplejson.load( indexFile, namedtuple_as_object = True )

            elif serializationBackend in [ 'cbor', 'rapidjson', 'ujson' ]:
                import importlib
                module = importlib.import_module( serializationBackend )
                self.fileIndex = getattr( module, 'load' )( indexFile )

            else:
                print( "Tried to load index path with unsupported serializationBackend:", serializationBackend, "!" )
                return

        if printDebug >= 2:
            def countDictEntries( d ):
                n = 0
                for value in d.values():
                    n += countDictEntries( value ) if isinstance( value, dict ) else 1
                return n
            print( "Files:", countDictEntries( self.fileIndex ) )

        t1 = timer()
        if printDebug >= 1:
            print( "Loading offset dictionary from", indexFileName, "took {:.2f}s".format( t1 - t0 ) )

    def tryLoadIndex( self, indexFileName ):
        """calls loadIndex if index is not loaded already and provides extensive error handling"""

        if self.indexIsLoaded():
            return True

        if not os.path.isfile( indexFileName ):
            return False

        if os.path.getsize( indexFileName ) == 0:
            try:
                os.remove( indexFileName )
            except OSError:
                print( "[Warning] Failed to remove empty old cached index file:", indexFileName )

            return False

        try:
            self.loadIndex( indexFileName )
        except Exception:
            self.fileIndex = None

            traceback.print_exc()
            print( "[Warning] Could not load file '" + indexFileName  )

            print( "[Info] Some likely reasons for not being able to load the index file:" )
            print( "[Info]   - Some dependencies are missing. Please isntall them with:" )
            print( "[Info]       pip3 --user -r requirements.txt" )
            print( "[Info]   - The file has incorrect read permissions" )
            print( "[Info]   - The file got corrupted because of:" )
            print( "[Info]     - The program exited while it was still writing the index because of:" )
            print( "[Info]       - the user sent SIGINT to force the program to quit" )
            print( "[Info]       - an internal error occured while writing the index" )
            print( "[Info]       - the disk filled up while writing the index" )
            print( "[Info]     - Rare lowlevel corruptions caused by hardware failure" )

            print( "[Info] This might force a time-costly index recreation, so if it happens often and "
                   "mounting is slow, try to find out why loading fails repeatedly, "
                   "e.g., by opening an issue on the public github page." )

            try:
                os.remove( indexFileName )
            except OSError:
                print( "[Warning] Failed to remove corrupted old cached index file:", indexFileName )

        return self.indexIsLoaded()


# Must be global so pickle can find out!
FileInfo = IndexedTar.FileInfo


class TarMount( fuse.Operations ):
    """
    This class implements the fusepy interface in order to create a mounted file system view
    to a TAR archive.
    This class can and is relatively thin as it only has to create and manage an IndexedTar
    object and query it for directory or file contents.
    It also adds a layer over the file permissions as all files must be read-only even
    if the TAR reader reports the file as originally writable because no TAR write support
    is planned.
    """

    def __init__(
        self,
        pathToMount,
        clearIndexCache = False,
        recursive = False,
        serializationBackend = None,
        gzipSeekPointSpacing = 4*1024*1024,
        mountPoint = None
    ):
        self.indexedTar = self._openTar( pathToMount, clearIndexCache, recursive,
                                         serializationBackend, gzipSeekPointSpacing )
        self.tarFile = self.indexedTar.tarFileObject

        # make the mount point read only and executable if readable, i.e., allow directory listing
        # @todo In some cases, I even get 2(!) '.' directories listed with ls -la!
        #       But without this, the mount directory is owned by root
        tarStats = os.stat( pathToMount )
        # clear higher bits like S_IFREG and set the directory bit instead
        mountMode = ( tarStats.st_mode & 0o777 ) | stat.S_IFDIR
        if mountMode & stat.S_IRUSR != 0: mountMode |= stat.S_IXUSR
        if mountMode & stat.S_IRGRP != 0: mountMode |= stat.S_IXGRP
        if mountMode & stat.S_IROTH != 0: mountMode |= stat.S_IXOTH
        self.rootFileInfo = SQLiteIndexedTar.FileInfo(
            offset       = 0                ,
            offsetheader = 0                ,
            size         = tarStats.st_size ,
            mtime        = tarStats.st_mtime,
            mode         = mountMode        ,
            type         = tarfile.DIRTYPE  ,
            linkname     = ""               ,
            uid          = tarStats.st_uid  ,
            gid          = tarStats.st_gid  ,
            istar        = True             ,
            issparse     = False
        )

        # Create mount point if it does not exist
        self.mountPointWasCreated = False
        if mountPoint and not os.path.exists( mountPoint ):
            os.mkdir( mountPoint )
            self.mountPointWasCreated = True
        self.mountPoint = os.path.abspath( mountPoint )

    def __del__( self ):
        try:
            if self.mountPointWasCreated:
                os.rmdir( self.mountPoint )
        except:
            pass

    def _openTar( self, tarFilePath, clearIndexCache, recursive, serializationBackend, gzipSeekPointSpacing ):
        if SQLiteIndexedTar._detectCompression( name = tarFilePath ) and serializationBackend != 'sqlite':
            print( "[Warning] Only the SQLite backend has .tar.bz2 and .tar.gz support, therefore will use that!" )
            serializationBackend = 'sqlite'

        # To be deprecated
        if serializationBackend != 'sqlite':
            return IndexedTar( tarFilePath,
                               writeIndex           = True,
                               clearIndexCache      = clearIndexCache,
                               recursive            = recursive,
                               serializationBackend = serializationBackend )

        return SQLiteIndexedTar( tarFilePath,
                                 writeIndex           = True,
                                 clearIndexCache      = clearIndexCache,
                                 recursive            = recursive,
                                 gzipSeekPointSpacing = gzipSeekPointSpacing  )

    @overrides( fuse.Operations )
    def getattr( self, path, fh = None ):
        if path == '/':
            fileInfo = self.rootFileInfo
        else:
            fileInfo = self.indexedTar.getFileInfo( path, listDir = False )

        if fileInfo is None or (
           not isinstance( fileInfo, IndexedTar.FileInfo ) and
           not isinstance( fileInfo, SQLiteIndexedTar.FileInfo ) ):
            raise fuse.FuseOSError( fuse.errno.ENOENT )

        # Dereference hard links
        if not stat.S_ISREG( fileInfo.mode ) and not stat.S_ISLNK( fileInfo.mode ) and fileInfo.linkname:
            return self.getattr( '/' + fileInfo.linkname.lstrip( '/' ), fh )

        # dictionary keys: https://pubs.opengroup.org/onlinepubs/007904875/basedefs/sys/stat.h.html
        statDict = dict( ( "st_" + key, getattr( fileInfo, key ) ) for key in ( 'size', 'mtime', 'mode', 'uid', 'gid' ) )
        # signal that everything was mounted read-only
        statDict['st_mode'] &= ~( stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH )
        statDict['st_mtime'] = int( statDict['st_mtime'] )
        statDict['st_nlink'] = 2

        return statDict

    @overrides( fuse.Operations )
    def readdir( self, path, fh ):
        # we only need to return these special directories. FUSE automatically expands these and will not ask
        # for paths like /../foo/./../bar, so we don't need to worry about cleaning such paths
        yield '.'
        yield '..'

        dirInfo = self.indexedTar.getFileInfo( path, listDir = True )

        if isinstance( dirInfo, dict ):
            for key in dirInfo.keys():
                yield key

    @overrides( fuse.Operations )
    def readlink( self, path ):
        fileInfo = self.indexedTar.getFileInfo( path )
        if fileInfo is None or (
           not isinstance( fileInfo, IndexedTar.FileInfo ) and
           not isinstance( fileInfo, SQLiteIndexedTar.FileInfo ) ):
            raise fuse.FuseOSError( fuse.errno.ENOENT )

        return fileInfo.linkname

    @overrides( fuse.Operations )
    def read( self, path, length, offset, fh ):
        fileInfo = self.indexedTar.getFileInfo( path )
        if fileInfo is None or (
           not isinstance( fileInfo, IndexedTar.FileInfo ) and
           not isinstance( fileInfo, SQLiteIndexedTar.FileInfo ) ):
            raise fuse.FuseOSError( fuse.errno.ENOENT )

        # Dereference hard links
        if not stat.S_ISREG( fileInfo.mode ) and not stat.S_ISLNK( fileInfo.mode ) and fileInfo.linkname:
            targetLink = '/' + fileInfo.linkname.lstrip( '/' )
            if targetLink != path:
                return self.read( targetLink, length, offset, fh )

        if isinstance( fileInfo, SQLiteIndexedTar.FileInfo ) and fileInfo.issparse:
            # The TAR file format is very simple. It's just a concatenation of TAR blocks. There is not even a
            # global header, only the TAR block headers. That's why we can simpley cut out the TAR block for
            # the sparse file using StenciledFile and then use tarfile on it to expand the sparse file correctly.
            tarBlockSize = fileInfo.offset - fileInfo.offsetheader + fileInfo.size
            tarSubFile = StenciledFile( self.tarFile, [ ( fileInfo.offsetheader, tarBlockSize ) ] )
            tmpTarFile = tarfile.open( fileobj = tarSubFile, mode = 'r:' )
            tmpFileObject = tmpTarFile.extractfile( next( iter( tmpTarFile ) ) )
            tmpFileObject.seek( offset, os.SEEK_SET )
            result = tmpFileObject.read( length )
            tmpTarFile.close()
            return result

        try:
            self.tarFile.seek( fileInfo.offset + offset, os.SEEK_SET )
            return self.tarFile.read( length )
        except RuntimeError as e:
            traceback.print_exc()
            print( "Caught exception when trying to read data from underlying TAR file! Returning errno.EIO." )
            raise fuse.FuseOSError( fuse.errno.EIO )


class TarFileType:
    """
    Similar to argparse.FileType but raises an exception if it is not a valid TAR file.
    """

    def __init__( self, mode = 'r', compressions = [ '' ] ):
        self.compressions = [ '' if c is None else c for c in compressions ]
        self.mode = mode

    def __call__( self, tarFile ):
        for compression in self.compressions:
            try:
                return ( tarfile.open( tarFile, mode = self.mode + ':' + compression ), compression )
            except tarfile.ReadError:
                None

        raise argparse.ArgumentTypeError(
            "Archive '{}' can't be opened!\n"
            "This might happen for xz compressed TAR archives, which currently is not supported.\n"
            "If you are trying to open a bz2 or gzip compressed file make sure that you have the indexed_bzip2 "
            "and indexed_gzip modules installed.".format( tarFile ) )


def parseArgs( args = None ):
    parser = argparse.ArgumentParser(
        formatter_class = argparse.ArgumentDefaultsHelpFormatter,
        description = '''\
        In order to reduce the mounting time, the created index for random access to files inside the tar will be saved to <path to tar>.index.<backend>[.<compression]. If it can't be saved there, it will be saved in ~/.ratarmount/<path to tar: '/' -> '_'>.index.<backend>[.<compression].
        ''' )

    parser.add_argument(
        '-f', '--foreground', action='store_true', default = False,
        help = 'Keeps the python program in foreground so it can print debug '
               'output when the mounted path is accessed.' )

    parser.add_argument(
        '-d', '--debug', type = int, default = 1,
        help = 'Sets the debugging level. Higher means more output. Currently, 3 is the highest.' )

    parser.add_argument(
        '-c', '--recreate-index', action='store_true', default = False,
        help = 'If specified, pre-existing .index files will be deleted and newly created.' )

    parser.add_argument(
        '-r', '--recursive', action='store_true', default = False,
        help = 'Mount TAR archives inside the mounted TAR recursively. '
               'Note that this only has an effect when creating an index. '
               'If an index already exists, then this option will be effectively ignored. '
               'Recreate the index if you want change the recursive mounting policy anyways.' )

    parser.add_argument(
        '-s', '--serialization-backend', type = str, default = 'sqlite',
        help =
        '(deprecated) Specify which library to use for writing out the TAR index. Supported keywords: (' +
        ','.join( IndexedTar.availableSerializationBackends + [ 'sqlite' ] ) + ')[.(' +
        ','.join( IndexedTar.availableCompressions ).strip( ',' ) + ')]' )

    # Considerations for the default value:
    #   - seek times for the bz2 backend are between 0.01s and 0.1s
    #   - seek times for the gzip backend are roughly 1/10th compared to bz2 at a default spacing of 4MiB
    #     -> we could do a spacing of 40MiB (however the comparison are for another test archive, so it might not apply)
    #   - ungziping firefox 66 inflates the compressed size of 66MiB to 184MiB (~3 times more) and takes 1.4s on my PC
    #     -> to have a response time of 0.1s, it would require a spacing < 13MiB
    #   - the gzip index takes roughly 32kiB per seek point
    #   - the bzip2 index takes roughly 16B per 100-900kiB of compressed data
    #     -> for the gzip index to have the same space efficiency assuming a compression ratio of only 1,
    #        the spacing would have to be 1800MiB at which point it would become almost useless
    parser.add_argument(
        '-gs', '--gzip-seek-point-spacing', type = float, default = 16,
        help =
        'This only is applied when the index is first created or recreated with the -c option. '
        'The spacing given in MiB specifies the seek point distance in the uncompressed data. '
        'A distance of 16MiB means that archives smaller than 16MiB in uncompressed size will '
        'not benefit from faster seek times. A seek point takes roughly 32kiB. '
        'So, smaller distances lead to more responsive seeking but may explode the index size!' )

    parser.add_argument(
        '-p', '--prefix', type = str, default = '',
        help = '[deprecated] Use "-o modules=subdir,subdir=<prefix>" instead. '
               'This standard way utilizes FUSE itself and will also work for other FUSE '
               'applications. So, it is preferable even if a bit more verbose.'
               'The specified path to the folder inside the TAR will be mounted to root. '
               'This can be useful when the archive as created with absolute paths. '
               'E.g., for an archive created with `tar -P cf /var/log/apt/history.log`, '
               '-p /var/log/apt/ can be specified so that the mount target directory '
               '>directly< contains history.log.' )

    parser.add_argument(
        '-o', '--fuse', type = str, default = '',
        help = 'Comma separated FUSE options. See "man mount.fuse" for help. '
               'Example: --fuse "allow_other,entry_timeout=2.8,gid=0". ' )

    parser.add_argument(
        '-v', '--version', action='store_true', help = 'Print version string.' )

    parser.add_argument(
        'tarfilepath', metavar = 'tar-file-path',
        type = TarFileType( 'r', [ '', 'bz2', 'gz' ] ), nargs = 1,
        help = 'The path to the TAR archive to be mounted.' )
    parser.add_argument(
        'mountpath', metavar = 'mount-path', nargs = '?',
        help = 'The path to a folder to mount the TAR contents into. '
               'If no mount path is specified, the TAR will be mounted to a folder of the same name '
               'but without a file extension.' )

    args = parser.parse_args( args )

    args.gzip_seek_point_spacing = args.gzip_seek_point_spacing * 1024 * 1024

    return args

def cli( args = None ):
    tmpArgs = sys.argv if args is None else args
    if '--version' in tmpArgs or '-v' in tmpArgs:
        print( "ratarmount", __version__ )
        return

    args = parseArgs( args )

    tarToMount = os.path.abspath( args.tarfilepath[0][0].name )

    # Convert the comma separated list of key[=value] options into a dictionary for fusepy
    fusekwargs = dict( [ option.split( '=', 1 ) if '=' in option else ( option, True )
                       for option in args.fuse.split( ',' ) ] ) if args.fuse else {}
    if args.prefix:
        fusekwargs['modules'] = 'subdir'
        fusekwargs['subdir'] = args.prefix

    mountPath = args.mountpath
    if mountPath is None:
        for ext in [ '.tar', '.tar.bz2', '.tbz2', '.tar.gz', '.tgz' ]:
            if tarToMount[-len( ext ):].lower() == ext.lower():
                mountPath = tarToMount[:-len( ext )]
                break
        if not mountPath:
            mountPath = os.path.splitext( tarToMount )[0]

    global printDebug
    printDebug = args.debug

    fuseOperationsObject = TarMount(
        pathToMount          = tarToMount,
        clearIndexCache      = args.recreate_index,
        recursive            = args.recursive,
        serializationBackend = args.serialization_backend,
        gzipSeekPointSpacing = args.gzip_seek_point_spacing,
        mountPoint           = mountPath )

    fuse.FUSE( operations = fuseOperationsObject,
               mountpoint = mountPath,
               foreground = args.foreground,
               nothreads  = args.serialization_backend == 'sqlite',
               **fusekwargs )

if __name__ == '__main__':
    cli( sys.argv[1:] )
