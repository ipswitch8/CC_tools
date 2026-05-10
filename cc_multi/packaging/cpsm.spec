# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for CPSM — one-folder build.
# Generated for PyInstaller >= 6.5
# Usage: pyinstaller --noconfirm packaging/cpsm.spec

import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Locate the project root (parent of this spec's directory)
# ---------------------------------------------------------------------------
spec_dir = Path(SPECPATH)          # noqa: F821  (SPECPATH injected by PyInstaller)
project_root = spec_dir.parent

# ---------------------------------------------------------------------------
# Data files — resources bundled alongside the binary
# ---------------------------------------------------------------------------
datas = [
    (str(project_root / "cpsm" / "resources" / "launcher_templates"), "cpsm/resources/launcher_templates"),
    (str(project_root / "cpsm" / "resources" / "icons"),               "cpsm/resources/icons"),
    (str(project_root / "cpsm" / "resources" / "translations"),        "cpsm/resources/translations"),
]

# ---------------------------------------------------------------------------
# Hidden imports needed at runtime but not detected by static analysis
# ---------------------------------------------------------------------------
hiddenimports = [
    # Pydantic v2 internals
    "pydantic.deprecated.decorator",
    "pydantic.deprecated.class_validators",
    "pydantic.v1",
    # ruamel.yaml
    "ruamel.yaml.comments",
    "ruamel.yaml.representer",
    "ruamel.yaml.constructor",
    # cryptography
    "cryptography.hazmat.primitives.kdf.pbkdf2",
    "cryptography.hazmat.backends.openssl.backend",
    # keyring backends
    "keyring.backends",
    "keyring.backends.fail",
    "keyring.backends.null",
]

if sys.platform == "linux":
    hiddenimports += [
        "keyring.backends.SecretService",
        "keyring.backends.libsecret",
    ]
elif sys.platform == "win32":
    hiddenimports += [
        "keyring.backends.Windows",
        "keyring.backends.chainer",
    ]

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
a = Analysis(
    [str(project_root / "cpsm" / "__main__.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "_tkinter",
        "matplotlib",
        "numpy",
        "scipy",
        "pandas",
        "IPython",
        "notebook",
        "PIL",
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)  # noqa: F821

# ---------------------------------------------------------------------------
# EXE — the thin launcher / entry-point binary
# ---------------------------------------------------------------------------
exe = EXE(           # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,          # binaries go into the folder, not the exe
    name="cpsm",                    # produces  dist/cpsm/cpsm  (or cpsm.exe on Windows)
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,                   # CLI app — keep console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# ---------------------------------------------------------------------------
# COLLECT — assemble the one-folder dist
# ---------------------------------------------------------------------------
coll = COLLECT(      # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="cpsm",                    # → dist/cpsm/
)
