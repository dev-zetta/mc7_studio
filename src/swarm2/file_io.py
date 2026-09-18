"""Portable regular-file checks and directory synchronization."""

import errno
import os
from pathlib import Path
import stat


def open_regular_read(path):
    """Reject symlinks/special files and verify the opened file's identity."""
    before = Path(path).lstat()
    if not stat.S_ISREG(before.st_mode):
        raise OSError(errno.ELOOP, "Choose a regular file without symbolic links", str(path))
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        after = os.fstat(descriptor)
        current = Path(path).lstat()
        if not stat.S_ISREG(after.st_mode) or not stat.S_ISREG(current.st_mode) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino):
            raise OSError(errno.ELOOP, "The regular file changed while opening", str(path))
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def sync_directory(path):
    # Windows cannot open directory file descriptors. Callers still flush the
    # file to disk and atomically replace it before reaching this point.
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
