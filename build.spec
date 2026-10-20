# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置：onedir 模式（启动快），保留控制台（显示局域网 IP）
a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=[("templates", "templates"), ("static", "static")],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AI掌柜",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AI掌柜",
)
