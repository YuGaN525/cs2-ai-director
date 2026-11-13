"""Input timing adapter with injected OS/backend/clock dependencies.

The compatibility entry point supplies the existing Windows implementation.
This module imports neither Tk nor a native input package.
"""
from __future__ import annotations
from typing import Any, Optional


class InputCancelled(RuntimeError):
    pass

class InputControllerAdapter:
    def __init__(self, cfg: dict[str, Any], log: LogBus, authorization_gate=None, *, runtime) -> None:
        self._runtime = runtime
        self.cfg = cfg
        self.log = log
        self.authorization_gate = authorization_gate
        self.cancel_event = None
        self._action_cancel_event = None
        self.hotkey_started = False
        self._held_keys = set()
        self._held_order = []
        self._target = None
        self._seen_game = False
        self.physical_modifiers = None
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
        if self._runtime.HAS_PYDIRECTINPUT and self._runtime.pydirectinput is not None:
            try:
                self._runtime.pydirectinput.FAILSAFE = False  # 鼠标在屏幕角落时不要抛异常
                self._runtime.pydirectinput.PAUSE = 0.0
            except Exception:
                pass

    def _authorized(self):
        if ((self.cancel_event is not None and self.cancel_event.is_set())
                or (self._action_cancel_event is not None and self._action_cancel_event.is_set())):
            self.release_held_keys()
            return False
        if self.authorization_gate is None:
            return True  # Legacy local runtime; authenticated runtime injects a gate.
        if self.authorization_gate.allowed():
            return True
        self.release_held_keys()
        return False

    def _foreground_identity(self):
        # No positive cache at an input boundary. A window handle alone can be reused.
        hwnd = self._runtime.get_foreground_hwnd()
        identity = self._runtime.game_window_identity(hwnd, self.cfg)
        if not identity or self._runtime.get_foreground_hwnd() != hwnd:
            return None
        return identity

    def _guard(self):
        if not self._authorized():
            raise InputCancelled("输入已取消或授权不可用")
        if not self._target or self._foreground_identity() != self._target:
            raise InputCancelled("CS2 已失焦或目标实例变化，停止本次输入；切回游戏后恢复")
        owned = {key for _, key in self._held_keys}
        if self._modifier_conflict(owned):
            raise InputCancelled("检测到用户修饰键，停止本次自动输入")

    def _modifier_conflict(self, owned):
        if self.physical_modifiers is not None:
            return self.physical_modifiers.conflict() or self._runtime.modifier_conflict(owned)
        return self._runtime.modifier_conflict(owned)

    def _wait(self, seconds):
        # Keep MulNX timing, but make every wait cancellable in <=20ms slices.
        remaining = max(0.0, seconds)
        self._guard()
        while remaining > 1e-9:
            step = min(0.02, remaining)
            self._runtime.time.sleep(step)
            remaining -= step
            self._guard()

    def _press(self, backend, key):
        self._guard()
        item = (backend, key)
        if item not in self._held_keys:
            self._held_keys.add(item)
            self._held_order.append(item)
        if backend == "direct":
            if self._runtime.pydirectinput.keyDown(key) is False:
                raise InputCancelled("系统未接收按键")
        else:
            self._runtime.keyboard.press(key)

    def _release(self, backend, key):
        # Cleanup is allowed after focus loss, only for this transaction's keys.
        item = (backend, key)
        if item not in self._held_keys:
            return
        if self.physical_modifiers is not None and self.physical_modifiers.is_down(key):
            # A physical press took ownership of this modifier during our hold.
            # Its physical key-up will clear OS state; do not release the user's key.
            self._held_keys.discard(item)
            self._held_order.remove(item)
            return
        if backend == "direct":
            if self._runtime.pydirectinput.keyUp(key) is False:
                raise InputCancelled("系统未接收按键释放")
        else:
            self._runtime.keyboard.release(key)
        self._held_keys.discard(item)
        self._held_order.remove(item)

    def release_held_keys(self):
        with self._send_lock:
            for backend, key in list(reversed(self._held_order)):
                try:
                    self._release(backend, key)
                except Exception:
                    pass  # Retain ownership for the next cleanup attempt.

    def _begin(self):
        self.release_held_keys()
        if self._held_keys or not self._authorized():
            return False
        if self._modifier_conflict(set()):
            self._log_focus_fail("用户正按住修饰键，跳过自动输入")
            return False
        if not self.ensure_foreground():
            return False
        self._target = self._foreground_identity()
        return self._target is not None

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
        # force retained for callers; both forms always query the OS afresh.
        return self._foreground_identity() is not None

    def ensure_foreground(self) -> bool:
        if self.foreground_is_game():
            self._seen_game = True
            return True
        # Auto-focus is only a startup convenience. Never fight a user's Alt+Tab
        # after observing the game; they must bring the game back themselves.
        if self._seen_game:
            self._log_focus_fail("CS2 已失焦，跳过输入；切回游戏后恢复")
            return False
        win = self._find_window(force=True)
        if win is None or not bool(self.cfg.get("auto_focus", True)):
            self._log_focus_fail("CS2 不在前台，跳过本次按键")
            return False
        self._runtime.activate_window(win.hwnd)
        for _ in range(8):
            if not self._authorized() or self._modifier_conflict(set()):
                return False
            if self.foreground_is_game():
                self._seen_game = True
                return True
            self._runtime.time.sleep(0.04)
        self._log_focus_fail("CS2 窗口激活失败，跳过本次按键")
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
            try:
                if not self._begin():
                    return False
                self.mark_self_press(key)
                self._press("direct", key)
                self._wait(0.03)
                self._release("direct", key)
                return True
            except InputCancelled as error:
                self._log_focus_fail(str(error))
                return False
            except Exception as error:
                self.log.warning(f"模拟按键失败：{type(error).__name__}")
                return False
            finally:
                self.release_held_keys()
                self._target = None

    def send_hotkey(self, hotkey: str, double: bool = False, *, cancel_event=None) -> bool:
        """发送 MulNX 快捷键；double=True 时连按两次（间隔可配）。"""
        self.hotkey_started = False
        if (cancel_event is not None and cancel_event.is_set()) or not self._authorized():
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

        if not keys:
            return False
        with self._send_lock:
            self._action_cancel_event = cancel_event
            try:
                if not self._begin():
                    return False
                if self._runtime.HAS_KEYBOARD and self._runtime.keyboard is not None:
                    if len(keys) == 1:
                        for mod in mods:
                            self._press("keyboard", mod)
                        if mods:
                            self._wait(lead)
                        for i in range(presses):
                            if i:
                                self._wait(max(0.0, interval - hold))
                            self._press("keyboard", keys[0])
                            self.hotkey_started = True
                            self._wait(hold)
                            self._release("keyboard", keys[0])
                        if mods:
                            self._wait(hold_after)
                    else:
                        for i in range(presses):
                            if i:
                                self._wait(interval)
                            for key in mods + keys:
                                self._press("keyboard", key)
                                if key in keys: self.hotkey_started = True
                            for key in reversed(mods + keys):
                                self._release("keyboard", key)
                    return True
                if len(hotkey) == 1 and self._runtime.HAS_PYDIRECTINPUT and self._runtime.pydirectinput is not None:
                    for i in range(presses):
                        if i:
                            self._wait(interval)
                        self._press("direct", hotkey.lower())
                        self.hotkey_started = True
                        self._wait(hold)
                        self._release("direct", hotkey.lower())
                    return True
                self.log.warning("未安装 keyboard，无法发送组合快捷键")
                return False
            except InputCancelled as error:
                self._log_focus_fail(str(error))
                return False
            except Exception as error:
                self.log.warning(f"发送快捷键失败：{type(error).__name__}")
                return False
            finally:
                self.release_held_keys()
                self._target = None
                self._action_cancel_event = None
