"""First-run setup: create the user folders and an editable config.toml."""

from __future__ import annotations

import logging
import shutil

from xauassist.app.paths import AppPaths
from xauassist.config.models import AppConfig
from xauassist.config.writer import save_config

log = logging.getLogger(__name__)


def ensure_first_run(paths: AppPaths) -> list[str]:
    """Create missing folders/files. Returns a description of what was created (empty if nothing).

    config.toml is copied from the documented example (so users get the comments) and falls back
    to a freshly written default config if the example is not bundled. Existing files are never
    touched, and credentials are never written here.
    """
    created: list[str] = []
    for folder in (paths.user_dir, paths.log_dir, paths.db_path.parent):
        if not folder.exists():
            folder.mkdir(parents=True, exist_ok=True)
            created.append(f"folder {folder}")

    example = paths.app_dir / "config" / "config.example.toml"
    if not paths.config_path.exists():
        if example.is_file():
            shutil.copyfile(example, paths.config_path)
        else:
            save_config(AppConfig(), paths.config_path)
        created.append(f"config {paths.config_path}")

    secrets_example = paths.app_dir / "config" / "secrets.example.toml"
    target = paths.user_dir / "secrets.example.toml"
    if secrets_example.is_file() and not target.exists():
        shutil.copyfile(secrets_example, target)
        created.append(f"file {target}")

    for item in created:
        log.info("First run: created %s", item)
    return created
