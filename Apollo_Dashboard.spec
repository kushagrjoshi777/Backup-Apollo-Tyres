# -*- mode: python ; coding: utf-8 -*-

import os
import sys

base_dir = os.path.abspath("dashboard")

datas = [
    (os.path.join(base_dir, "static"), "static"),
    (os.path.abspath("src"), "src"),
]

hiddenimports = [
    "cv2",
    "numpy",
    "scipy",
    "scipy.ndimage",
    "scipy.fft",
    "pywt",
    "reportlab",
    "reportlab.lib",
    "reportlab.platypus",
    "PIL",
    "flask",
    "werkzeug",
]

excludes = [
    "torch",
    "torchvision",
    "tensorflow",
    "tensorboard",
    "ultralytics",
    "plotly",
    "pandas",
    "matplotlib",
    "tkinter",
    "pyarrow",
    "fsspec",
    "urllib3",
    "IPython",
    "jupyter",
    "altair",
    "pydantic",
    "rich",
    "pygments",
    "sympy",
]

a = Analysis(
    [os.path.join(base_dir, "app.py")],
    pathex=[base_dir, os.path.abspath("src"), os.path.abspath(".")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="Apollo_Tyre_Analysis",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
