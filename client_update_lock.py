"""OS-owned locks: a crashed process releases them without stale PID guessing."""
import os
from pathlib import Path
import time


class FileLock:
    def __init__(self,path,*,shared=False,timeout=2):
        self.path=Path(path);self.shared=shared;self.timeout=timeout;self.stream=None

    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.stream=self.path.open('a+b')
        deadline=time.monotonic()+self.timeout
        if os.name=='nt':
            import ctypes
            from ctypes import wintypes
            import msvcrt
            class Overlapped(ctypes.Structure):
                _fields_=[('Internal',ctypes.c_size_t),('InternalHigh',ctypes.c_size_t),
                          ('Offset',wintypes.DWORD),('OffsetHigh',wintypes.DWORD),('hEvent',wintypes.HANDLE)]
            self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            self.kernel.LockFileEx.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(Overlapped)]
            self.kernel.UnlockFileEx.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(Overlapped)]
            self.handle=msvcrt.get_osfhandle(self.stream.fileno());self.overlapped=Overlapped()
            def acquire():
                if self.kernel.LockFileEx(self.handle,1|(0 if self.shared else 2),0,1,0,ctypes.byref(self.overlapped)):return True
                error=ctypes.get_last_error()
                if error!=33:raise ctypes.WinError(error)
                return False
        else:
            import fcntl
            def acquire():
                try:
                    fcntl.flock(self.stream,fcntl.LOCK_NB|(fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX));return True
                except BlockingIOError:return False
        try:
            while not acquire():
                if time.monotonic()>=deadline:raise TimeoutError('update_busy')
                time.sleep(.05)
            return self
        except BaseException:
            self.stream.close();self.stream=None;raise

    def __exit__(self,*_):
        if self.stream is not None:
            try:
                if os.name=='nt':
                    import ctypes
                    self.kernel.UnlockFileEx(self.handle,0,1,0,ctypes.byref(self.overlapped))
                else:
                    import fcntl
                    fcntl.flock(self.stream,fcntl.LOCK_UN)
            finally:self.stream.close();self.stream=None
