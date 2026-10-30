"""Build the standalone app and verify it.

    python packaging/build.py                 # one-folder build + self-check + zip
    python packaging/build.py --onefile       # single XAUAssistant.exe
    python packaging/build.py --test          # run the test suite first
    python packaging/build.py --no-zip

Run it on Windows to produce the real .exe. Requires: pip install -e ".[gui,mt5,secrets,build]"
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
BUILD = ROOT / "build"
NAME = "XAUAssistant"


def run(cmd: list[str], env: dict[str, str] | None = None) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True, env={**os.environ, **(env or {})})


def executable_path(onefile: bool) -> Path:
    suffix = ".exe" if sys.platform == "win32" else ""
    return DIST / f"{NAME}{suffix}" if onefile else DIST / NAME / f"{NAME}{suffix}"


def selfcheck(exe: Path, data_dir: Path) -> bool:
    """Run the packaged app's --selfcheck against a scratch data folder; print its report."""
    if data_dir.exists():
        shutil.rmtree(data_dir)
    env = {**os.environ, "QT_QPA_PLATFORM": os.environ.get("QT_QPA_PLATFORM", "offscreen")}
    proc = subprocess.run(
        [str(exe), "--selfcheck", "--data-dir", str(data_dir)],
        cwd=ROOT, env=env, timeout=180, capture_output=True, text=True,
    )  # fmt: skip
    report = data_dir / "logs" / "selfcheck.txt"
    print(report.read_text() if report.exists() else proc.stdout + proc.stderr)
    return proc.returncode == 0


def make_zip(onefile: bool) -> Path:
    archive = DIST / f"{NAME}-windows.zip" if sys.platform == "win32" else DIST / f"{NAME}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        if onefile:
            exe = executable_path(True)
            zf.write(exe, exe.name)
        else:
            for path in sorted((DIST / NAME).rglob("*")):
                if path.is_file():
                    zf.write(path, Path(NAME) / path.relative_to(DIST / NAME))
        zf.write(
            ROOT / "docs" / "INSTALL_WINDOWS.md",
            f"{NAME}/README.md" if not onefile else "README.md",
        )
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--onefile", action="store_true")
    parser.add_argument("--test", action="store_true", help="run pytest before building")
    parser.add_argument("--no-zip", action="store_true")
    args = parser.parse_args()

    if args.test:
        run([sys.executable, "-m", "pytest", "-q"], {"QT_QPA_PLATFORM": "offscreen"})
    run(
        [sys.executable, "-m", "PyInstaller", str(ROOT / "packaging" / "xauassist.spec"),
         "--noconfirm", "--clean", "--distpath", str(DIST), "--workpath", str(BUILD)],
        {"XAU_ONEFILE": "1" if args.onefile else "0"},
    )  # fmt: skip
    exe = executable_path(args.onefile)
    if not exe.exists():
        print(f"build failed: {exe} not found", file=sys.stderr)
        return 1
    print(f"\nBuilt {exe}")
    if not selfcheck(exe, BUILD / "selfcheck-data"):
        print("SELF-CHECK FAILED: do not ship this build", file=sys.stderr)
        return 2
    if not args.no_zip:
        print(f"Archive: {make_zip(args.onefile)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
