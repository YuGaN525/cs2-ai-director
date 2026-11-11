"""Track ONLY physical modifier state; ignore injected and all non-modifier keys.

No text, scan codes, window names or event history are retained. The hook never
suppresses input. Failure to install disables automatic input, not user controls.
"""
import ctypes
import os
import threading

MODIFIERS = {0x10: 'shift', 0xA0: 'shift', 0xA1: 'shift',
             0x11: 'ctrl', 0xA2: 'ctrl', 0xA3: 'ctrl',
             0x12: 'alt', 0xA4: 'alt', 0xA5: 'alt', 0x5B: 'win', 0x5C: 'win'}
ALIASES = {'control': 'ctrl', 'windows': 'win', 'cmd': 'win'}


class PhysicalModifiers:
    def __init__(self):
        self.lock = threading.Lock()
        self.down = set()
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.started = False
        self.thread = None
        self.thread_id = 0
        self._user32 = None

    def event(self, vk, flags, message, scan_code=0):
        # LLKHF_INJECTED (and lower-integrity injected) never changes physical state.
        if vk not in MODIFIERS or flags & 0x12: return
        if vk == 0x10: vk = 0xA1 if scan_code == 0x36 else 0xA0
        elif vk == 0x11: vk = 0xA3 if flags & 1 else 0xA2
        elif vk == 0x12: vk = 0xA5 if flags & 1 else 0xA4
        with self.lock:
            if message in (0x100, 0x104): self.down.add(vk)
            elif message in (0x101, 0x105): self.down.discard(vk)

    def conflict(self):
        with self.lock: return not self.started or bool(self.down)

    def is_down(self, key):
        family = ALIASES.get(key, key)
        # Generic keyboard modifiers use the left scan code. A user's right
        # modifier must not prevent cleanup of our distinct left modifier.
        vk = {'alt': 0xA4, 'ctrl': 0xA2, 'shift': 0xA0, 'win': 0x5B}.get(family)
        with self.lock: return vk in self.down

    def start(self):
        if os.name != 'nt': raise RuntimeError('物理修饰键保护需要 Windows')
        self.thread = threading.Thread(target=self._run, name='physical-modifier-guard', daemon=True)
        self.thread.start()
        if not self.ready.wait(2) or not self.started:
            self.close()
            raise RuntimeError('物理修饰键保护未就绪，已阻止自动输入')

    def _run(self):
        from ctypes import wintypes as w
        user = self._user32 = ctypes.WinDLL('user32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        result_type = ctypes.c_ssize_t
        callback_type = ctypes.WINFUNCTYPE(result_type, ctypes.c_int, w.WPARAM, w.LPARAM)
        class Event(ctypes.Structure):
            _fields_ = [('vkCode', w.DWORD), ('scanCode', w.DWORD), ('flags', w.DWORD), ('time', w.DWORD), ('extra', ctypes.c_size_t)]
        user.SetWindowsHookExW.argtypes = [ctypes.c_int, callback_type, w.HINSTANCE, w.DWORD]
        user.SetWindowsHookExW.restype = w.HHOOK
        user.CallNextHookEx.argtypes = [w.HHOOK, ctypes.c_int, w.WPARAM, w.LPARAM]
        user.CallNextHookEx.restype = result_type
        user.UnhookWindowsHookEx.argtypes = [w.HHOOK]
        user.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
        user.GetMessageW.restype = ctypes.c_int
        user.PeekMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]
        user.PostThreadMessageW.argtypes = [w.DWORD, w.UINT, w.WPARAM, w.LPARAM]
        user.GetAsyncKeyState.argtypes = [ctypes.c_int]; user.GetAsyncKeyState.restype = ctypes.c_short
        kernel.GetCurrentThreadId.restype = w.DWORD
        kernel.GetModuleHandleW.argtypes = [w.LPCWSTR]; kernel.GetModuleHandleW.restype = w.HMODULE
        self.thread_id = kernel.GetCurrentThreadId()
        message = w.MSG()
        user.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)  # Create the thread queue before readiness.
        @callback_type
        def callback(code, message, pointer):
            try:
                if code >= 0:
                    event = ctypes.cast(pointer, ctypes.POINTER(Event)).contents
                    if event.vkCode in MODIFIERS:
                        self.event(event.vkCode, event.flags, message, event.scanCode)
            except Exception:
                self.started = False
            return user.CallNextHookEx(None, code, message, pointer)
        handle = user.SetWindowsHookExW(13, callback, kernel.GetModuleHandleW(None), 0)
        if not handle:
            self.ready.set(); return
        try:
            with self.lock:
                # Seed left/right keys only; avoid stale generic modifier aliases.
                self.down = {vk for vk in MODIFIERS if vk not in (0x10, 0x11, 0x12) and user.GetAsyncKeyState(vk) & 0x8000}
                self.started = True
            self.ready.set()
            message = w.MSG()
            if self.stop.is_set(): return
            while user.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                pass  # Low-level callbacks are dispatched by GetMessage.
        finally:
            user.UnhookWindowsHookEx(handle)
            self.started = False

    def close(self):
        self.stop.set()
        if self.thread and self.thread.is_alive() and self.thread_id and self._user32:
            self._user32.PostThreadMessageW(self.thread_id, 0x12, 0, 0)
            self.thread.join(2)
        self.started = False
