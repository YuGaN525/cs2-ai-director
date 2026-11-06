# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules, collect_all
import os
import json
from pathlib import Path
from public_trust import read_public_trust

datas = [('assets', 'assets')]
public_trust_path = os.environ.get('DIRECTOR_PUBLIC_TRUST_CONFIG')
if public_trust_path:
    trust=read_public_trust(public_trust_path)
    staged=Path('build/public-trust/identity-server.json')
    staged.parent.mkdir(parents=True,exist_ok=True)
    staged.write_text(json.dumps(trust,sort_keys=True),encoding='utf-8')
    datas.append((str(staged.resolve()), '.'))
binaries = []
hiddenimports = ['obsws_python', 'websocket']
for package in ('sklearn', 'joblib', 'demoparser2'):
    package_data, package_binaries, package_imports = collect_all(package)
    datas += package_data
    binaries += package_binaries
    hiddenimports += package_imports
hiddenimports += collect_submodules('obsws_python')


a = Analysis(
    ['cs2_ai_director.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'pytest', 'IPython'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Yuzi_CS2_AIDirector',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/yuzi.ico'],
)
