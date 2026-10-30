"""Where the app finds its assets and keeps its files (dev checkout or PyInstaller bundle)."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

APP_FOLDER_NAME = "XAUAssistant"


@dataclass(frozen=True, slots=True)
class AppPaths:
    app_dir: Path  # read-only resources: assets/sounds/..., config/*.example.toml
    user_dir: Path  # writable: config.toml, secrets.toml, data/, logs/

    @property
    def config_path(self) -> Path:
        return self.user_dir / "config.toml"

    @property
    def secrets_path(self) -> Path:
        return self.user_dir / "secrets.toml"

    @property
    def db_path(self) -> Path:
        return self.user_dir / "data" / "signals.db"

    @property
    def log_dir(self) -> Path:
        return self.user_dir / "logs"


def is_writable(directory: Path) -> bool:
    """True if files can be created in `directory` (created if missing)."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write_test"
        probe.write_text("x")
        probe.unlink()
    except OSError:
        return False
    return True


def fallback_user_dir(env: Mapping[str, str]) -> Path:
    """Per-user writable location used when the program folder is read-only (Program Files)."""
    base = env.get("APPDATA") or env.get("XDG_DATA_HOME")
    return Path(base) / APP_FOLDER_NAME if base else Path.home() / f".{APP_FOLDER_NAME.lower()}"


def default_paths(
    *,
    frozen: bool | None = None,
    executable: str | None = None,
    bundle_dir: str | None = None,
    env: Mapping[str, str] | None = None,
) -> AppPaths:
    """Resolve app/user folders. Arguments exist so every case is testable on any OS.

    - Development: both are the repository root.
    - Packaged: resources come from the PyInstaller bundle (`_MEIPASS` for one-file builds, the
      program folder for one-folder builds). Settings, logs and the signal database live NEXT TO
      the executable ("portable") when that folder is writable, otherwise in the per-user
      %APPDATA% folder, so installing under Program Files does not break the app.
    """
    is_frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if not is_frozen:
        root = Path(__file__).resolve().parents[3]
        return AppPaths(app_dir=root, user_dir=root)
    exe = Path(executable or sys.executable).resolve()
    bundle = Path(bundle_dir) if bundle_dir else Path(getattr(sys, "_MEIPASS", exe.parent))
    user = exe.parent
    if not is_writable(user):
        user = fallback_user_dir(env if env is not None else os.environ)
    return AppPaths(app_dir=bundle, user_dir=user)
