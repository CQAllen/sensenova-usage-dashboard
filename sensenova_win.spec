# -*- mode: python ; coding: utf-8 -*-
# Windows onefile 打包配置（由 build_win.py 调用：pyinstaller --noconfirm sensenova_win.spec）
# 与根目录的 SenseNova用量查询.spec（macOS .app）并行存在，互不覆盖。
from PyInstaller.utils.hooks import collect_all

datas = [('web', 'web')]          # 前端资源（index.html / styles.css / app.js），相对本 spec 所在目录解析
binaries = []
hiddenimports = [
    'webview.platforms.edgechromium', 'webview.platforms.winforms',
    'requests', 'jwcrypto', 'cryptography',
    'auth_login', 'credstore', 'accounts', 'sensenova_api', 'imagegen',
]
for pkg in ('webview', 'pythonnet', 'clr_loader'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ['dashboard.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# onefile：把 binaries/datas 直接塞进 EXE，不用 COLLECT
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='SenseNova用量查询',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                       # --windowed：无控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='app.ico',
    version='version_info.txt',
)
