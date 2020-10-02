#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import bisect
import copy
import io
import os
import stat
import sys
import tempfile

import fuse

def overrides( parentClass ):
    def overrider( method ):
        assert method.__name__ in dir( parentClass )
        assert callable( getattr( parentClass, method.__name__ ) )
        return method
    return overrider

def ceilDiv( a, b ):
    return ( a + b - 1 ) // b

class JoinedFile(io.BufferedIOBase):
    """A file abstraction layer giving a joined view to one file split into parts."""

    def __init__( self, filePaths ):
        self.filePaths = []
        self.sizes = []
        self.fileobj = None
        self.currentFile = None

        for path in filePaths:
            if not os.path.isfile( path ):
                raise Exception( "File {} does not exist!".format( path ) )
            size = os.stat( path ).st_size
            if size > 0:
                self.filePaths.append( os.path.abspath( path ) )
                self.sizes.append( size )

        # Calculate cumulative sizes
        self.cumsizes = [ 0 ]
        for size in self.sizes:
            assert size > 0
            self.cumsizes.append( self.cumsizes[-1] + size )

        # Seek to the first stencil offset in the underlying file so that "read" will work out-of-the-box
        self.seek( 0 )

    def _findStencil( self, offset ):
        """
        Return index to file to which the offset belongs to. E.g., for file sizes [5,2], offsets 0 to
        and including 4 will still be inside the first file, i.e., index 0 will be returned. For offset 6,
        index 1 would be returned because it now is in the second file.
        """
        # bisect_left( value ) gives an index for a lower range: value < x for all x in list[0:i]
        # Because value >= 0 and list starts with 0 we can therefore be sure that the returned i>0
        # Consider the file sizes [2,2,2] -> cumsizes [0,2,4,6]. Seek to offset 2 should seek to the second entry.
        assert offset >= 0
        i = bisect.bisect_left( self.cumsizes, offset + 1 ) - 1
        assert i >= 0
        return i

    @overrides(io.BufferedIOBase)
    def close(self):
        self.fileobj.close()
        self.fileobj = None
        self.currentFile = None

    @overrides(io.BufferedIOBase)
    def fileno(self):
        if self.fileobj is not None:
            return self.fileobj.fileno()
        return -1

    @overrides(io.BufferedIOBase)
    def seekable(self):
        return True

    @overrides(io.BufferedIOBase)
    def readable(self):
        return True

    @overrides(io.BufferedIOBase)
    def writable(self):
        return False

    @overrides(io.BufferedIOBase)
    def read(self, size=-1):
        if size == -1:
            size = self.cumsizes[-1] - self.offset

        # This loop works in a kind of leapfrog fashion. On each even loop iteration it opens the next file
        # and on each odd iteration it reads the data and increments the offset inside the file!
        result = b''
        i = self._findStencil( self.offset )
        while size > 0 and i < len( self.sizes ):
            # Read as much as requested or as much as the current file contains
            readableSize = min( size, self.sizes[i] - ( self.offset - self.cumsizes[i] ) )
            if readableSize == 0:
                # Go to next file
                i += 1
                if i >= len( self.filePaths ):
                    break

                self.fileobj.close()
                self.fileobj = open( self.filePaths[i], 'rb' )
            else:
                # Actually read data
                tmp = self.fileobj.read( readableSize )
                self.offset += len( tmp )
                result += tmp
                size -= readableSize
                # Now, either size is 0 or readableSize will be 0 in the next iteration

        return result

    @overrides(io.BufferedIOBase)
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
        offsetInsideFile = self.offset - self.cumsizes[i]
        assert offsetInsideFile >= 0
        assert offsetInsideFile < self.sizes[i]

        # After determining where to seek to, actually do it
        if i != self.currentFile:
            if self.fileobj is not None and not self.fileobj.closed:
                self.fileobj.close()
            self.fileobj = open( self.filePaths[i], 'rb' )

        self.fileobj.seek( offsetInsideFile, io.SEEK_SET )

        return self.offset

    @overrides(io.BufferedIOBase)
    def tell(self):
        return self.offset

def testJoinedFile():
    tmpDir = tempfile.TemporaryDirectory()
    fileSizes = [2,2,2,4,8,1]
    filePaths = [ os.path.join( tmpDir.name, str(i) ) for i in range( len( fileSizes ) ) ]
    i = 0
    for path, size in zip( filePaths, fileSizes ):
        with open( path, 'wb' ) as file:
            file.write( ''.join( [ chr( i + j ) for j in range( size ) ] ).encode() )
        i += size

    print( "Test JoinedFile._findStencil" )
    joinedFile = JoinedFile( filePaths )
    expectedResults = [ 0,0, 1,1, 2,2, 3,3,3,3, 4,4,4,4,4,4,4,4, 5 ]
    for offset, iExpectedStencil in enumerate( expectedResults ):
        assert joinedFile._findStencil( offset ) == iExpectedStencil

    print( "Test JoinedFile with single file" )

    assert JoinedFile( [ filePaths[0] ] ).read( 1 ) == b"\x00"
    assert JoinedFile( [ filePaths[0] ] ).read( 2 ) == b"\x00\x01"
    assert JoinedFile( [ filePaths[0] ] ).read() == b"\x00\x01"

    print( "Test JoinedFile using two files" )

    joinedFile = JoinedFile( filePaths[:2] )
    assert joinedFile.read() == b"\x00\x01\x02\x03"
    for i in [0,1,2,3,2,1,0,2,0,2]:
        joinedFile.seek( i )
        joinedFile.tell() == i
        assert joinedFile.read( 1 ) == chr( i ).encode()
    joinedFile.seek( 0, io.SEEK_END )
    assert joinedFile.tell() == 4


class FuseFileObjectMount( fuse.Operations ):
    def __init__( self, fileobj, mountPoint, stats ):
        self.fileobj = fileobj
        self.stats = stats
        self.fileName = 'joined'

        # Get file size
        oldPos = self.fileobj.tell()
        self.fileobj.seek( 0, io.SEEK_END )
        self.stats['st_size'] = fileobj.tell()
        self.stats['st_blocks'] = ceilDiv( self.stats['st_size'], self.stats['st_blksize'] )
        del self.stats['st_ino']
        self.fileobj.seek( oldPos )

        # Create mount point if it does not exist
        self.mountPointWasCreated = False
        if mountPoint and not os.path.exists( mountPoint ):
            os.mkdir( mountPoint )
            self.mountPointWasCreated = True
        self.mountPoint = os.path.realpath( mountPoint )

    def __del__( self ):
        try:
            if self.mountPointWasCreated:
                os.rmdir( self.mountPoint )
        except:
            pass

    @overrides( fuse.Operations )
    def getattr( self, path, fh = None ):
        if path == '/':
            stats = copy.deepcopy( self.stats )
            stats['st_mode'] = 0o777 | stat.S_IFDIR
            stats['st_size'] = 0
            return stats

        if path == '/' + self.fileName:
            return self.stats

        raise fuse.FuseOSError( fuse.errno.ENOENT )

    @overrides( fuse.Operations )
    def readdir( self, path, fh ):
        if path != '/':
            raise fuse.FuseOSError( fuse.errno.ENOENT )
        return [ '.', '..', self.fileName ]

    @overrides( fuse.Operations )
    def readlink( self, path ):
        return ""

    @overrides( fuse.Operations )
    def read( self, path, length, offset, fh ):
        if path != '/' + self.fileName:
            raise fuse.FuseOSError( fuse.errno.ENOENT )

        try:
            self.fileobj.seek( offset, os.SEEK_SET )
            return self.fileobj.read( length )
        except RuntimeError as e:
            traceback.print_exc()
            print( "Caught exception when trying to read data from underlying TAR file! Returning errno.EIO." )
            raise fuse.FuseOSError( fuse.errno.EIO )

def cli( args = None ):
    if not args:
        testJoinedFile()
        return

    assert len( args ) >= 2
    mountPoint = args[-1]
    joinedFile = JoinedFile( args[:-1] )

    stats = os.stat( args[0] )
    stats = dict( [ field, getattr( stats, field ) ] for field in dir( stats ) if field.startswith( 'st_' ) )

    fuseOperationsObject = FuseFileObjectMount( joinedFile, mountPoint, stats )

    print( "Join", args[:-1], "into", os.path.join( mountPoint + '/' + fuseOperationsObject.fileName ) )

    fuse.FUSE( operations = fuseOperationsObject,
               mountpoint = mountPoint,
               foreground = False,
               nothreads  = True,
               debug      = False )

if __name__ == '__main__':
    cli( sys.argv[1:] )
