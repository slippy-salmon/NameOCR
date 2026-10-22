# PyInstaller recipe for NameOCR (one-folder build, no console window).
# Run from the repo root:  pyinstaller packaging/NameOCR.spec --noconfirm
# Set TESSERACT_DIR to a folder holding tesseract.exe, its DLLs and
# tessdata\eng.traineddata to bundle Tesseract with the app.
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
TESS = os.environ.get("TESSERACT_DIR", "")

a = Analysis(
    [os.path.join(ROOT, "app.py")],
    pathex=[ROOT],
    datas=[(os.path.join(ROOT, "icon.png"), "."), (os.path.join(ROOT, "icon.ico"), ".")],
    hiddenimports=["pynput.keyboard._win32", "pynput.mouse._win32"],
    # never needed; keeps stray packages on the build machine out of the bundle
    excludes=["tkinter", "matplotlib", "pandas", "scipy", "lxml", "cryptography", "IPython",
              "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
              "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtMultimedia", "PySide6.QtPdf"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="NameOCR",
    icon=os.path.join(ROOT, "icon.ico"),
    console=False,
    upx=False,
)
extra = [Tree(TESS, prefix="tesseract")] if TESS else []
coll = COLLECT(exe, a.binaries, a.datas, *extra, name="NameOCR", upx=False)
