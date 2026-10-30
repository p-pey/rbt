"""Credential storage. Telegram credentials never live in source code or config.toml.

Order of preference: OS keyring (Windows Credential Manager) if the `keyring` package and a usable
backend exist, otherwise a git-ignored `secrets.toml` (owner-only permissions where supported).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

SERVICE_NAME = "xauassist"
_TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")
_CHAT_RE = re.compile(r"-?\d+|@[A-Za-z][A-Za-z0-9_]{3,}")


@dataclass(frozen=True, slots=True)
class TelegramSecrets:
    bot_token: str = field(repr=False)  # repr=False: never printed by accident
    chat_id: str

    def __repr__(self) -> str:
        return f"TelegramSecrets(bot_token=<hidden>, chat_id={self.chat_id!r})"

    @property
    def is_complete(self) -> bool:
        return bool(self.bot_token.strip() and self.chat_id.strip())


def validate_telegram_credentials(token: str, chat_id: str) -> list[str]:
    """Human-readable problems with the credential format (empty list = looks valid)."""
    problems: list[str] = []
    if not _TOKEN_RE.fullmatch(token.strip()):
        problems.append("Bot token should look like 123456789:AAE... (from @BotFather).")
    if not _CHAT_RE.fullmatch(chat_id.strip()):
        problems.append("Chat id should be a number (e.g. 123456789 or -100123...) or @channel.")
    return problems


class SecretStore(Protocol):
    def load(self) -> TelegramSecrets | None: ...

    def save(self, secrets: TelegramSecrets) -> None: ...

    def clear(self) -> None: ...


class FileSecretStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> TelegramSecrets | None:
        try:
            with self._path.open("rb") as fh:
                table: dict[str, Any] = tomllib.load(fh).get("telegram", {})
        except FileNotFoundError:
            return None
        except (OSError, tomllib.TOMLDecodeError) as exc:
            log.warning("Cannot read %s: %s", self._path, exc)
            return None
        token, chat = table.get("bot_token"), table.get("chat_id")
        if not isinstance(token, str) or not isinstance(chat, str):
            return None
        return TelegramSecrets(token, chat)

    def save(self, secrets: TelegramSecrets) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        text = (
            "[telegram]\n"
            f"bot_token = {json.dumps(secrets.bot_token)}\n"  # JSON strings are valid TOML strings
            f"chat_id = {json.dumps(secrets.chat_id)}\n"
        )
        # Create with owner-only permissions before any secret is written (no-op on Windows).
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)


class KeyringSecretStore:
    """Backed by the `keyring` module (injected so it is testable and optional)."""

    def __init__(self, keyring_module: Any) -> None:
        self._kr = keyring_module

    def load(self) -> TelegramSecrets | None:
        token = self._kr.get_password(SERVICE_NAME, "telegram_bot_token")
        chat = self._kr.get_password(SERVICE_NAME, "telegram_chat_id")
        if not token or not chat:
            return None
        return TelegramSecrets(token, chat)

    def save(self, secrets: TelegramSecrets) -> None:
        self._kr.set_password(SERVICE_NAME, "telegram_bot_token", secrets.bot_token)
        self._kr.set_password(SERVICE_NAME, "telegram_chat_id", secrets.chat_id)

    def clear(self) -> None:
        for name in ("telegram_bot_token", "telegram_chat_id"):
            try:
                self._kr.delete_password(SERVICE_NAME, name)
            except Exception:  # noqa: BLE001 - "not found" differs per backend
                log.debug("keyring entry %s not present", name)


class ChainedSecretStore:
    """Use the primary store; fall back to the secondary when the primary fails or is empty."""

    def __init__(self, primary: SecretStore, fallback: SecretStore) -> None:
        self._primary = primary
        self._fallback = fallback

    def load(self) -> TelegramSecrets | None:
        try:
            found = self._primary.load()
            if found is not None:
                return found
        except Exception as exc:  # noqa: BLE001 - keyring backends raise assorted errors
            log.warning("Primary secret store unavailable (%s); using fallback", type(exc).__name__)
        return self._fallback.load()

    def save(self, secrets: TelegramSecrets) -> None:
        try:
            self._primary.save(secrets)
            self._fallback.clear()  # do not leave a plaintext copy behind
        except Exception as exc:  # noqa: BLE001
            log.warning("Primary secret store unavailable (%s); saving to file", type(exc).__name__)
            self._fallback.save(secrets)

    def clear(self) -> None:
        for store in (self._primary, self._fallback):
            try:
                store.clear()
            except Exception:  # noqa: BLE001
                log.debug("clearing a secret store failed", exc_info=True)


def default_secret_store(secrets_path: Path) -> SecretStore:
    """Keyring (if installed) with `secrets.toml` fallback, else just the file."""
    file_store = FileSecretStore(secrets_path)
    try:
        import keyring  # type: ignore[import-not-found,unused-ignore]
    except ImportError:
        return file_store
    return ChainedSecretStore(KeyringSecretStore(keyring), file_store)
