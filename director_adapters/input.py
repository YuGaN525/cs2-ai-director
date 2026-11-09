"""Input timing adapter with injected OS/backend/clock dependencies.

The compatibility entry point supplies the existing Windows implementation.
This module imports neither Tk nor a native input package.
"""
from __future__ import annotations
from typing import Any, Optional

class InputControllerAdapter:
    def __init__(self, cfg: dict[str, Any], log: LogBus, authorization_gate=None, *, runtime) -> None:
        self._runtime = runtime
        self.cfg = cfg
        self.log = log
        self.authorization_gate = authorization_gate
        self.cancel_event = None
        self._held_keys = set()
        # 两个锁分开：钩子回调只碰 _suppress_lock，绝不能被“正在发送按键”
        # 的 _send_lock 阻塞，否则低级钩子会拖慢/卡住系统输入。
        self._send_lock = self._runtime.threading.RLock()
        self._suppress_lock = self._runtime.threading.Lock()
        # 概念上等价于全局 simulating_until；这里按数字键分别记录，
        # 避免把用户在抑制窗口内按下的“其他数字键”也一起吞掉。
        self._simulating_until: dict[str, float] = {}
        self._window_cache: Optional[WindowInfo] = None
        self._window_cache_at = 0.0
        self._last_focus_fail_log = 0.0
        self._proc_cache_at = 0.0
        self._proc_cache_ok = False
        if self._runtime.HAS_PYDIRECTINPUT and self._runtime.pydirectinput is not None:
            try:
                self._runtime.pydirectinput.FAILSAFE = False  # 鼠标在屏幕角落时不要抛异常
                self._runtime.pydirectinput.PAUSE = 0.0
            except Exception:
                pass

    def _authorized(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            self.release_held_keys()
            return False
        if self.authorization_gate is None:
            return True  # Legacy local runtime; authenticated runtime injects a gate.
        if self.authorization_gate.allowed():
            return True
        self.release_held_keys()
        return False

    def _press(self, backend, key):
        if not self._authorized():
            raise RuntimeError("设备离线授权不可用，请联网校验")
        self._held_keys.add((backend, key))
        if backend == "direct":
            self._runtime.pydirectinput.keyDown(key)
        else:
            self._runtime.keyboard.press(key)

    def _release(self, backend, key):
        if backend == "direct":
            self._runtime.pydirectinput.keyUp(key)
        else:
            self._runtime.keyboard.release(key)
        self._held_keys.discard((backend, key))

    def release_held_keys(self):
        with self._send_lock:
            for backend, key in list(self._held_keys):
                try:
                    self._release(backend, key)
                except Exception:
                    pass

    # ---- 自捕获抑制 ----
    def mark_self_press(self, digit: str) -> None:
        with self._suppress_lock:
            self._simulating_until[digit] = self._runtime.time.monotonic() + float(self.cfg["self_press_guard"])

    def is_self_press(self, digit: str) -> bool:
        with self._suppress_lock:
            return self._runtime.time.monotonic() < self._simulating_until.get(digit, 0.0)

    # ---- 窗口 ----
    def _find_window(self, force: bool = False) -> Optional[WindowInfo]:
        now = self._runtime.time.monotonic()
        if (not force and self._window_cache is not None
                and now - self._window_cache_at < 2.0
                and self._runtime.is_window(self._window_cache.hwnd)):
            return self._window_cache
        win = self._runtime.find_cs2_window(self.cfg)
        self._window_cache = win
        self._window_cache_at = now
        return win

    def foreground_is_game(self, force: bool = False) -> bool:
        # 注意：激活窗口后必须 force=True 重新查询，否则 0.4s 缓存会把
        # “刚激活成功”误判成失败，导致第一次切换被跳过。
        now = self._runtime.time.monotonic()
        if not force and now - self._proc_cache_at < 0.4:
            return self._proc_cache_ok
        hwnd = self._runtime.get_foreground_hwnd()
        ok = bool(hwnd) and self._runtime.is_game_process(hwnd)
        self._proc_cache_at = now
        self._proc_cache_ok = ok
        return ok

    def ensure_foreground(self) -> bool:
        """
        按键前必须保证 CS2 在前台。找不到窗口或激活失败就跳过并记录，
        绝不盲目发送按键（否则数字键会落到浏览器/聊天框里）。
        """
        if self.foreground_is_game():
            return True
        win = self._find_window(force=True)
        if win is None:
            self._log_focus_fail("未找到 CS2 窗口（cs2.exe），跳过本次按键")
            return False
        if not bool(self.cfg.get("auto_focus", True)):
            self._log_focus_fail("CS2 不在前台且已关闭自动聚焦，跳过本次按键")
            return False
        self._runtime.activate_window(win.hwnd)
        for _ in range(8):
            if self.foreground_is_game(force=True):
                return True
            self._runtime.time.sleep(0.04)
        self._log_focus_fail("CS2 窗口激活失败（可能被系统前台锁拦截），跳过本次按键")
        return False

    def _log_focus_fail(self, msg: str) -> None:
        now = self._runtime.time.monotonic()
        if now - self._last_focus_fail_log > 3.0:
            self._last_focus_fail_log = now
            self.log.warning(msg)

    # ---- 按键 ----
    def press_slot(self, slot: int) -> bool:
        if not self._authorized():
            return False
        key = self._runtime.slot_to_key(slot)
        if key is None:
            return False
        if bool(self.cfg.get("dry_run")):
            self.log.info(
                f"[演练] 模拟按下观察者切换键 {key}（GSI slot={int(slot)}）")
            return True
        if not self._runtime.HAS_PYDIRECTINPUT or self._runtime.pydirectinput is None:
            self.log.warning("pydirectinput 未安装，无法模拟按键（请先安装依赖）")
            return False
        with self._send_lock:
            if not self.ensure_foreground():
                return False
            self.mark_self_press(key)
            try:
                self._press("direct", key)
                self._runtime.time.sleep(0.03)  # 至少跨 1-2 帧，避免游戏轮询错过按键
                self._release("direct", key)
            except Exception as e:
                self.release_held_keys()
                self.log.warning(f"模拟数字键 {key} 失败：{type(e).__name__}: {e}")
                return False
        return True

    def send_hotkey(self, hotkey: str, double: bool = False) -> bool:
        """发送 MulNX 快捷键；double=True 时连按两次（间隔可配）。"""
        if not self._authorized():
            return False
        hotkey = (hotkey or "").strip()
        if not hotkey:
            return False
        mode_txt = "双击" if double else "单次"
        if bool(self.cfg.get("dry_run")):
            self.log.info(f"[演练] 模拟 MulNX 快捷键 {hotkey}（{mode_txt}）")
            return True
        try:
            interval = max(0.03, min(0.5, float(self.cfg.get("hotkey_double_interval", 0.08))))
            hold = max(0.02, min(0.2, float(self.cfg.get("hotkey_press_hold", 0.03))))
            lead = max(0.0, min(0.3, float(self.cfg.get("hotkey_modifier_lead", 0.08))))
            release_delay = max(0.0, min(0.3, float(
                self.cfg.get("hotkey_modifier_release_delay", 0.05))))
            hold_after = max(0.0, min(1.0, float(
                self.cfg.get("hotkey_modifier_hold_after", 0.30))))
        except (TypeError, ValueError):
            interval, hold, lead = 0.08, 0.03, 0.08
            release_delay, hold_after = 0.05, 0.30
        # MulNX 的 200ms 连击缓冲要求 Alt 在最后一次主键后仍保持按下；
        # 兼容旧配置：取 release_delay 与新 hold_after 的较大值。
        hold_after = max(release_delay, hold_after)
        presses = 2 if double else 1
        mods, keys = self._runtime.split_hotkey(hotkey)

        with self._send_lock:
            if not self.ensure_foreground():
                return False
            if self._runtime.HAS_KEYBOARD and self._runtime.keyboard is not None:
                try:
                    if len(keys) == 1:
                        # 关键：修饰键只在开头按下、结尾释放；双击是“按住 Alt，
                        # 连点两次主键”。既符合 comboClick 语义，也保证 keydown
                        # 默认保持 30ms，避免相机工具用 GetAsyncKeyState 轮询时漏掉。
                        for mod in mods:
                            self._press("keyboard", mod)
                        if mods:
                            self._runtime.time.sleep(lead)  # 按住 Alt 80ms 再按主键
                        try:
                            for i in range(presses):
                                if i:
                                    # interval 是"两次主键按下"的间隔；
                                    # 第一次已按住 hold，所以再等 interval-hold。
                                    self._runtime.time.sleep(max(0.0, interval - hold))
                                self._press("keyboard", keys[0])
                                self._runtime.time.sleep(hold)
                                self._release("keyboard", keys[0])
                            if mods:
                                # MulNX 在 200ms 后才消费 ComboClick；这里默认 300ms
                                self._runtime.time.sleep(hold_after)
                        finally:
                            for mod in reversed(mods):
                                try:
                                    self._release("keyboard", mod)
                                except Exception:
                                    pass
                    else:
                        # 非常规组合（多个主键）退回整体发送
                        for i in range(presses):
                            if i:
                                self._runtime.time.sleep(interval)
                            for key in mods + keys:
                                self._press("keyboard", key)
                            for key in reversed(mods + keys):
                                self._release("keyboard", key)
                    return True
                except Exception as e:
                    self.release_held_keys()
                    self.log.warning(
                        f"发送快捷键 {hotkey}（{mode_txt}）失败：{type(e).__name__}: {e}"
                    )
                    return False
        # 没有 keyboard 时只支持单键
        if len(hotkey) == 1 and self._runtime.HAS_PYDIRECTINPUT and self._runtime.pydirectinput is not None:
            try:
                for i in range(presses):
                    if i:
                        self._runtime.time.sleep(interval)
                    self._press("direct", hotkey.lower())
                    self._runtime.time.sleep(hold)
                    self._release("direct", hotkey.lower())
                return True
            except Exception as e:
                self.release_held_keys()
                self.log.warning(
                    f"发送单键 {hotkey}（{mode_txt}）失败：{type(e).__name__}: {e}"
                )
                return False
        self.log.warning("未安装 keyboard，无法发送组合快捷键")
        return False
