"""Application control chords; hook threads enqueue intent, the Tk thread executes it."""
from collections import deque
import re
import threading
import time

CONTROLS = {
    'director': ('director_toggle_hotkey', '启动 / 停止导播', 'ctrl+alt+d'),
    'cinematics': ('cinematics_toggle_hotkey', '开启 / 关闭运镜', 'ctrl+alt+m'),
    'pause': ('pause_hotkey', '暂停事件', 'ctrl+alt+p'),
}
ALIASES = {'control':'ctrl', 'win':'windows', 'cmd':'windows'}
MODIFIERS = ('ctrl', 'alt', 'shift', 'windows')


def canonical_chord(value):
    value = str(value or '').strip().lower()
    if not value: return ''
    parts = [ALIASES.get(p.strip(), p.strip()) for p in value.split('+')]
    if any(not p or not re.fullmatch(r'[a-z0-9_ ]+', p) for p in parts):
        raise ValueError('快捷键必须是一个组合键，例如 Ctrl+Alt+D；不支持连续组合。')
    mods = [p for p in parts if p in MODIFIERS]
    keys = [p for p in parts if p not in MODIFIERS]
    if len(mods) != len(set(mods)) or len(keys) != 1:
        raise ValueError('快捷键需要一个主键，修饰键不能重复。')
    return '+'.join([m for m in MODIFIERS if m in mods] + keys)


def control_bindings(cfg, parser=None):
    reserved = {'alt+p'}  # MulNX single stop; double clears previews.
    for key in ('hlae_spawn_ct_key','hlae_spawn_t_key','hlae_bomb_a_key','hlae_bomb_b_key'):
        value = cfg.get(key)
        if value: reserved.add(canonical_chord(value))
    result, seen = {}, set()
    for action, (field, label, default) in CONTROLS.items():
        chord = canonical_chord(cfg.get(field, default))
        if chord:
            if '+' not in chord and not re.fullmatch(r'f(?:[1-9]|1[0-9]|2[0-4])', chord):
                raise ValueError(label+'请使用组合键或 F1～F24，避免打字或切人时误触。')
            if chord in reserved or chord in seen:
                raise ValueError(label+'与其他控制键或 MulNX 运镜键冲突，请更换。')
            if parser:
                try: parser(chord)
                except (ValueError, KeyError): raise ValueError(label+'快捷键无法识别：'+chord) from None
            seen.add(chord)
        result[action] = chord
    return result


class ControlHotkeys:
    def __init__(self, backend, clock=time.monotonic):
        self.backend, self.clock = backend, clock
        self.lock = threading.Lock()
        self.generation = 0
        self.closed = True
        self.handles, self.queue, self.pending, self.last = [], deque(), set(), {}
        self.registered = {}

    def close(self):
        with self.lock:
            self.generation += 1
            self.closed = True
            handles, self.handles = self.handles, []
            self.queue.clear(); self.pending.clear(); self.last.clear()
            self.registered = {}
        for handle in handles:
            try: self.backend.remove_hotkey(handle)
            except Exception: pass

    def configure(self, cfg):
        bindings = control_bindings(cfg, getattr(self.backend, 'parse_hotkey', None))
        self.close()
        with self.lock:
            self.closed = False
            generation = self.generation
        failures = []
        for action, chord in bindings.items():
            if not chord: continue
            if self.backend is None:
                failures.append(CONTROLS[action][1]); continue
            try:
                callback = lambda a=action, g=generation: self.post(a, g)
                handle = self.backend.add_hotkey(chord, callback, suppress=False, trigger_on_release=True)
                self.handles.append(handle)
                self.registered[action] = chord
            except Exception: failures.append(CONTROLS[action][1])
        return failures

    def post(self, action, generation):
        now = self.clock()
        with self.lock:
            if (self.closed or generation != self.generation or action not in CONTROLS
                    or action in self.pending or now-self.last.get(action, -float('inf')) < .4):
                return
            self.last[action] = now
            self.pending.add(action)
            self.queue.append((action, now))  # At most one pending entry per known control.

    def drain(self):
        now = self.clock()
        with self.lock:
            events = list(self.queue)
            self.queue.clear(); self.pending.clear()
        # Do not replay commands after a modal dialog or a stalled UI.
        return [action for action, when in events if now-when <= 2.0]
