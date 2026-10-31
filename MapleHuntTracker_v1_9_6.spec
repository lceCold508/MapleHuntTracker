# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('assets/haste_buff_template.png', 'assets')]
binaries = []
hiddenimports = [
    'windows_capture', 'winrt.windows.media.ocr', 'winrt.windows.globalization',
    'winrt.windows.graphics.imaging', 'winrt.windows.storage.streams',
    'winrt.windows.foundation', 'winrt.runtime',
    'cv2', 'numpy',
]
for package in ('winocr', 'windows_capture'):
    tmp_ret = collect_all(package)
    datas += tmp_ret[0]
    binaries += tmp_ret[1]
    hiddenimports += tmp_ret[2]

a = Analysis(['overlay_app.py'], pathex=[], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports, hookspath=[], hooksconfig={},
             runtime_hooks=[], excludes=['customtkinter'], noarchive=False,
             optimize=0)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
          name='MapleHuntTracker_v1_9_6', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=True,
          upx_exclude=[], runtime_tmpdir=None, console=False,
          disable_windowed_traceback=False, argv_emulation=False,
          target_arch=None, codesign_identity=None, entitlements_file=None)
