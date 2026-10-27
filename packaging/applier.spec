# PyInstaller spec for the applier desktop app.
#
#     python -m PyInstaller packaging/applier.spec --noconfirm
#
# or, cross-platform and with the checks:  python packaging/build.py
#
# Produces a single self-contained executable. "Self-contained" means Python,
# every pure-Python dependency, the web UI, the LaTeX templates and the example
# configuration -- not Chrome and not a TeX distribution, which are hundreds of
# megabytes each and which the app detects and reports on instead.
#
# The user's own files never live in here. They live in the data directory
# chosen by applier.config.data_root(), because this bundle is unpacked to a
# temporary directory and deleted on exit.

import sys
from pathlib import Path

try:
    ROOT = Path(SPECPATH).resolve().parent          # noqa: F821  (PyInstaller global)
except NameError:                                    # running the file directly
    ROOT = Path(__file__).resolve().parent.parent

datas = [
    # The web UI. applier/web/server.py finds it relative to its own __file__,
    # which resolves correctly inside the bundle as long as it is shipped here.
    (str(ROOT / "applier" / "web" / "static"), "applier/web/static"),
    # Resume and cover-letter templates.
    (str(ROOT / "templates"), "templates"),
    # Seeds for a first run. The real settings.yaml ships so a new install has
    # working sources and gates; the profile ships only as an example, because
    # a profile is personal and must be filled in by whoever installs this.
    (str(ROOT / "config" / "settings.yaml"), "config"),
    (str(ROOT / "config" / "profile.example.yaml"), "config"),
    (str(ROOT / "config" / "bank" / "atoms.example.yaml"), "config/bank"),
    (str(ROOT / "config" / "bank" / "claims.example.yaml"), "config/bank"),
    (str(ROOT / "config" / "bank" / "master_resume.example.md"), "config/bank"),
    # The browser extension, so Settings can point at a real folder.
    (str(ROOT / "extension"), "extension"),
    (str(ROOT / "docs"), "docs"),
    # The privacy audit and the family check are run from the app.
    (str(ROOT / "scripts"), "scripts"),
]
datas = [(src, dst) for src, dst in datas if Path(src).exists()]

hiddenimports = [
    # Reached by name through the provider table rather than by import, so
    # static analysis cannot see them.
    "applier.apply.adapters",
    "applier.llm.router",
    # pywebview picks its backend at runtime.
    "webview",
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "webview.platforms.cocoa",
    "clr_loader",
    "pythonnet",
    # Optional at runtime, imported lazily; without these the frozen app
    # silently loses PDF verification, which is the one check that must not
    # be allowed to vanish quietly.
    "pdfplumber",
    "pypdf",
    "keyring.backends.Windows",
    "keyring.backends.macOS",
    "keyring.backends.SecretService",
]

excludes = [
    # Never imported, and each drags in tens of megabytes.
    "tkinter", "matplotlib", "numpy", "scipy", "pandas", "PIL", "IPython",
    "pytest", "PyInstaller", "pygame", "setuptools", "pip", "wheel",
]

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

icon = ROOT / "packaging" / "applier.ico"
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="applier",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                  # UPX trips antivirus heuristics for no real gain
    runtime_tmpdir=None,
    # No console window on Windows; on macOS this is ignored and the .app
    # bundle below is what gets double-clicked.
    console=False,
    disable_windowed_traceback=False,
    icon=str(icon) if icon.exists() else None,
)

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="applier.app",
        icon=str(ROOT / "packaging" / "applier.icns")
             if (ROOT / "packaging" / "applier.icns").exists() else None,
        bundle_identifier="dev.aliabdullah.applier",
        info_plist={
            "CFBundleName": "applier",
            "CFBundleDisplayName": "applier",
            "NSHighResolutionCapable": True,
            # The app talks to 127.0.0.1 over plain HTTP, which App Transport
            # Security blocks by default. Loopback only -- nothing here relaxes
            # transport security for any real host.
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        },
    )
