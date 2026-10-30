"""Guards that keep the packaging files consistent with the repository."""

import py_compile
import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SPEC = (REPO / "packaging" / "xauassist.spec").read_text()


def test_every_file_the_spec_bundles_exists() -> None:
    sources = re.findall(r'str\(ROOT / ((?:"[^"]+"(?: / )?)+)\)', SPEC)
    assert len(sources) >= 6
    for expr in sources:
        parts = re.findall(r'"([^"]+)"', expr)
        assert (REPO.joinpath(*parts)).exists(), f"spec references missing {'/'.join(parts)}"


def test_default_sounds_are_bundled_where_the_app_looks_for_them() -> None:
    from xauassist.config.models import SoundConfig

    cfg = SoundConfig()
    for configured in (cfg.buy_sound, cfg.sell_sound):
        folder, name = configured.rsplit("/", 1)
        assert f'"{folder}"' in SPEC and name in SPEC  # lands in the same relative folder


def test_icon_is_a_valid_multi_size_ico() -> None:
    data = (REPO / "assets" / "icon.ico").read_bytes()
    reserved, kind, count = data[0:2], data[2:4], int.from_bytes(data[4:6], "little")
    assert reserved == b"\x00\x00" and kind == b"\x01\x00" and count >= 5
    assert data[6 + 16 * count : 6 + 16 * count + 8] == b"\x89PNG\r\n\x1a\n"  # first image is a PNG


def test_build_scripts_compile_and_reference_existing_files() -> None:
    for script in ("packaging/build.py", "packaging/entry.py", "packaging/xauassist.spec"):
        text = (REPO / script).read_text()
        if script.endswith(".py"):
            py_compile.compile(str(REPO / script), doraise=True)
        else:
            compile(text, script, "exec")
    assert (REPO / "docs" / "INSTALL_WINDOWS.md").exists()  # build.py puts it in the archive
    assert "packaging\\build.py" in (REPO / "packaging" / "build_windows.bat").read_text()


def test_workflow_builds_with_the_documented_extras_and_runs_the_selfcheck() -> None:
    workflow = (REPO / ".github" / "workflows" / "build-windows.yml").read_text()
    assert "windows-latest" in workflow and "packaging/build.py" in workflow
    assert ".[gui,mt5,secrets,build,dev]" in workflow
    extras = tomllib.loads((REPO / "pyproject.toml").read_text())["project"][
        "optional-dependencies"
    ]
    assert {"gui", "mt5", "secrets", "build", "dev"} <= set(extras)
    assert (
        "MetaTrader5" in extras["mt5"][0] and "win32" in extras["mt5"][0]
    )  # Windows-only dependency


def test_windows_only_dependencies_are_conditional() -> None:
    project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    assert any(d.startswith("tzdata") and "win32" in d for d in project["dependencies"])


def test_executable_is_windowed_and_not_upx_packed() -> None:
    assert "console=False" in SPEC and "upx=False" in SPEC and "upx=True" not in SPEC


def test_spec_collects_dynamic_imports() -> None:
    for needle in ('collect_submodules("xauassist")', "keyring.backends", "tzdata", "MetaTrader5"):
        assert needle in SPEC
