# -*- mode: python ; coding: utf-8 -*-
# 对分易自动签到 — PyInstaller 打包配置（Windows / onedir / windowed）

import os

APP_NAME = "对分易自动签到"
ICON = os.path.join("assets", "icon.ico")

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    datas=[("web", "web")],
    # pyzbar 携带的 libzbar 动态库由 hooks-contrib 收集，这里显式声明模块
    # 入口，避免二维码解码依赖在打包产物中缺失。
    hiddenimports=["pyzbar", "pyzbar.pyzbar", "pyzbar.zbar_library"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "unittest",
        "pydoc_data",
        "PyQt5",
        "PySide6",
        "qtpy",
        "gi",
        "cefpython3",
        "cryptography",
        "trio",
        "websockets",
        "lxml",
        "setuptools",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # windowed：无控制台黑窗
    icon=ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)
