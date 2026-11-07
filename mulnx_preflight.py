"""Read-only MulNX macro binding inspection. Never launches or rewrites MulNX.

Accept only the flat scalar KCP format used by the current macro files. A
different YAML structure is reported as unknown, never guessed or executed.
"""
import hashlib
import re
from pathlib import Path

SCENES = {'CT': 'hlae_spawn_ct', 'T': 'hlae_spawn_t',
          'ASIDE': 'hlae_bomb_a', 'BSIDE': 'hlae_bomb_b'}
UPGRADE_HELP = ('CS2 更新后，按 MulNX 发布说明重新下载兼容版本。更新前备份已验证的整个运镜包；'
                '退出 MulNX 后，将原运镜包覆盖到新版本对应的 CamPacks 目录以恢复自定义快捷键。'
                '保留新下载的原始副本以便回退。重新选择安装目录和运镜包，核对快捷键后再进游戏确认镜头。'
                '文件匹配不代表当前游戏已加载该包，也不代表版本兼容或真实联动已通过。')


def _read(path):
    with Path(path).open('rb') as stream:
        data = stream.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError('配置超过读取上限')
    return data.decode('utf-8-sig'), hashlib.sha256(data).hexdigest()


def read_binding(path):
    text, digest = _read(path)
    fields = {}
    in_block = False
    seen = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if re.fullmatch(r'KCP:\s*(?:#.*)?', line):
            if seen:
                raise ValueError('重复 KCP 块')
            seen = in_block = True
            continue
        if in_block and not line[0].isspace():
            in_block = False
        if in_block:
            match = re.fullmatch(r'  (usable|ctrl|shift|alt|vkCode|comboClick):\s*(true|false|[0-9]+)\s*(?:#.*)?', line)
            if not match or match[1] in fields:
                raise ValueError('不支持或重复的 KCP 字段')
            key, value = match.groups()
            fields[key] = {'true': True, 'false': False}.get(value, value)
    if set(fields) != {'usable', 'ctrl', 'shift', 'alt', 'vkCode', 'comboClick'}:
        raise ValueError('KCP 字段不完整')
    for key in ('usable', 'ctrl', 'shift', 'alt'):
        if type(fields[key]) is not bool:
            raise ValueError('KCP 布尔类型不正确')
    for key in ('vkCode', 'comboClick'):
        if not isinstance(fields[key], str):
            raise ValueError('KCP 数字类型不正确')
        fields[key] = int(fields[key])
    return fields, digest


def expected_binding(key, mode):
    parts = str(key).lower().replace(' ', '').split('+')
    modifiers, main = parts[:-1], parts[-1]
    if len(set(modifiers)) != len(modifiers) or any(m not in ('ctrl', 'shift', 'alt') for m in modifiers):
        raise ValueError('不支持的修饰键')
    if len(main) == 1 and main.isascii() and main.isalnum():
        vk = ord(main.upper())
    elif re.fullmatch(r'f(?:[1-9]|1[0-9]|2[0-4])', main):
        vk = 111 + int(main[1:])
    else:
        raise ValueError('此主键需要人工核对')
    if mode not in ('single', 'double'):
        raise ValueError('未知触发模式')
    return dict(usable=True, ctrl='ctrl' in modifiers, shift='shift' in modifiers,
                alt='alt' in modifiers, vkCode=vk, comboClick=2 if mode == 'double' else 1)


def inspect(root, pack, config):
    root, pack = Path(root), Path(pack)
    result = {'launcher_found': (root/'CS2Injector'/'CS2Injector.exe').is_file(),
              'shortcut_enabled': None, 'scenes': {}, 'matched': False,
              'real_game_confirmed': False}
    expected_parent = (root/'CS2OBTool'/'CameraSystem'/'CamPacks').resolve()
    if pack.resolve().parent != expected_parent:
        raise ValueError('请选择此 MulNX 安装目录下 CamPacks 内的一个运镜包')
    try:
        text, digest = _read(root/'CS2OBTool'/'CameraSystem'/'Config'/'Config.yaml')
        values = re.findall(r'^    shortcutEnable: (true|false)\s*$', text, flags=re.M)
        if len(values) == 1:
            result['shortcut_enabled'] = values[0] == 'true'
        result['camera_config_sha256'] = digest
    except (OSError, ValueError):
        pass
    for name, prefix in SCENES.items():
        try:
            actual, digest = read_binding(pack/'CamMacros'/f'{name}.yaml')
            expected = expected_binding(config.get(prefix+'_key', ''), config.get(prefix+'_mode', 'double'))
            result['scenes'][name] = {'state': 'matched' if actual == expected else 'mismatch',
                                     'actual': actual, 'expected': expected, 'sha256': digest}
        except (OSError, ValueError):
            result['scenes'][name] = {'state': 'unknown', 'reason': '文件缺失、结构变化或快捷键不支持；请人工核对'}
    result['matched'] = (result['launcher_found'] and result['shortcut_enabled'] is True
                         and all(v['state'] == 'matched' for v in result['scenes'].values()))
    return result


def describe(result):
    lines = ['启动器：' + ('已找到' if result['launcher_found'] else '未找到'),
             '运镜快捷键总开关：' + {True: '开启', False: '关闭', None: '未知'}[result['shortcut_enabled']]]
    names = {'CT': 'CT 出生点', 'T': 'T 出生点', 'ASIDE': 'A 包点', 'BSIDE': 'B 包点'}
    for scene, item in result['scenes'].items():
        lines.append(names[scene] + '：' + {'matched': '文件绑定一致', 'mismatch': '绑定不一致', 'unknown': '需要人工核对'}[item['state']])
        if item['state'] == 'mismatch':
            for label, binding in [('程序', item['expected']), ('运镜包', item['actual'])]:
                modifiers = '+'.join(k for k in ('ctrl', 'shift', 'alt') if binding[k])
                lines.append(f"  {label}：{modifiers} + VK {binding['vkCode']}，{binding['comboClick']} 次；启用={binding['usable']}")
    lines += ['', '核对通过仅表示文件绑定一致；仍须在游戏中确认加载的地图包、版本兼容和镜头效果。', '', UPGRADE_HELP]
    return '\n'.join(lines)
