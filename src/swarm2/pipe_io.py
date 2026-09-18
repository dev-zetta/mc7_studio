"""Readiness for subprocess pipes on POSIX and Windows.

Windows select() only accepts sockets. PeekNamedPipe preserves the existing
bounded, incremental os.read() callers without buffering whole child outputs.
"""

import os
import select
import selectors
import time


class _WindowsPipeSelector:
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        import msvcrt

        self._ctypes = ctypes
        self._handle = msvcrt.get_osfhandle
        self._peek = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
        self._peek.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.LPDWORD, wintypes.LPDWORD, wintypes.LPDWORD]
        self._peek.restype = wintypes.BOOL
        self._keys = {}

    def register(self, fileobj, events, data=None):
        if events != selectors.EVENT_READ:
            raise ValueError("Only subprocess pipe reads are supported")
        fd = fileobj if isinstance(fileobj, int) else fileobj.fileno()
        if fd in self._keys:
            raise KeyError("Pipe is already registered")
        key = selectors.SelectorKey(fileobj, fd, events, data)
        self._keys[fd] = key
        return key

    def unregister(self, fileobj):
        fd = fileobj if isinstance(fileobj, int) else fileobj.fileno()
        return self._keys.pop(fd)

    def get_map(self):
        return self._keys

    def select(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        while True:
            ready = []
            for key in self._keys.values():
                available = self._ctypes.c_ulong()
                if self._peek(self._handle(key.fd), None, 0, None, self._ctypes.byref(available), None):
                    if available.value:
                        ready.append((key, selectors.EVENT_READ))
                else:
                    code = self._ctypes.get_last_error()
                    if code in (109, 232):  # Broken/closed pipe: let os.read see EOF.
                        ready.append((key, selectors.EVENT_READ))
                    else:
                        raise self._ctypes.WinError(code)
            if ready or not self._keys:
                return ready
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return []
            time.sleep(0.005 if remaining is None else min(0.005, remaining))

    def close(self):
        self._keys.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def pipe_selector():
    return _WindowsPipeSelector() if os.name == "nt" else selectors.DefaultSelector()


def pipe_readable(fd, timeout=0):
    if os.name != "nt":
        return bool(select.select([fd], [], [], timeout)[0])
    with pipe_selector() as ready:
        ready.register(fd, selectors.EVENT_READ)
        return bool(ready.select(timeout))
