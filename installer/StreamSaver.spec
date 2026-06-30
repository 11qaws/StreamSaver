# -*- mode: python ; coding: utf-8 -*-
import os, sys

SRC = os.path.abspath(os.path.join(SPECPATH, '..'))

a = Analysis(
    [os.path.join(SRC, 'main.py')],
    pathex=[SRC],
    binaries=[],
    datas=[
        (os.path.join(SRC, 'index.html'),          '.'),
        (os.path.join(SRC, 'watch_channels.json'), '.'),
    ],
    hiddenimports=[
        'pystray._win32',
        'PIL._imaging',
        'PIL.Image',
        'PIL.ImageDraw',
        'discord.app_commands',
        'discord.ext.commands',
        'discord.ext.tasks',
        'websockets',
        'websockets.server',
        'websockets.client',
        'websockets.legacy',
        'websockets.legacy.client',
        'websockets.legacy.server',
        'aiohttp',
        'win10toast',
        'tkinter',
        'tkinter.simpledialog',
        'wmi',
        'winreg',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'numpy', 'scipy', 'pandas'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='StreamSaver',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=os.path.join(SPECPATH, 'assets', 'icon.ico'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='StreamSaver',
)
