"""Build a standalone app for the current OS with PyInstaller.

    python build.py            installs requirements + PyInstaller, then builds
    python build.py --no-install

Output (in ./dist):
    Windows  VintedLabel4x6.exe
    macOS    VintedLabel4x6.app
    Linux    VintedLabel4x6
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "VintedLabel4x6"


def main():
    if "--no-install" not in sys.argv:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade",
                               "-r", str(ROOT / "requirements.txt"), "pyinstaller"])
    assets = ROOT / "assets"
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
        "--name", NAME,
        "--icon", str(assets / "icon.ico"),   # PyInstaller converts it for macOS
        "--add-data", f"{assets / 'icon.ico'}{os.pathsep}.",
        "--add-data", f"{assets / 'icon.png'}{os.pathsep}.",
        "--collect-all", "pypdfium2", "--collect-all", "pypdfium2_raw",
        "--collect-all", "customtkinter", "--collect-all", "tkinterdnd2",
        "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
    ]
    if sys.platform == "darwin":
        cmd += ["--osx-bundle-identifier", "io.github.vintedlabel4x6"]   # .app bundle
    else:
        cmd += ["--onefile"]                                               # single file
    cmd.append(str(ROOT / f"{NAME}.py"))
    subprocess.check_call(cmd)
    print(f"\nDone! Look in {ROOT / 'dist'}")


if __name__ == "__main__":
    main()
