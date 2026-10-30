import logging
import os
import stat
import threading
from pathlib import Path
from typing import Any

import pytest

from helpers import make_config, mirror, run, scenario_a_sell
from xauassist.app.logging_setup import RedactingFormatter, register_secret, setup_logging
from xauassist.config.models import SoundConfig
from xauassist.config.secrets import (
    ChainedSecretStore,
    FileSecretStore,
    KeyringSecretStore,
    TelegramSecrets,
    validate_telegram_credentials,
)
from xauassist.domain.enums import Direction
from xauassist.domain.signal import Signal
from xauassist.notify.base import NotificationDispatcher
from xauassist.notify.sound import SoundNotifier

REPO = Path(__file__).resolve().parents[1]
TOKEN = "123456789:AAEhBP0av28cEXAMPLEexampleEXAMPLE_abc12"


def sell() -> Signal:
    (s,) = run(scenario_a_sell(), make_config())
    return s


def buy() -> Signal:
    (s,) = run(mirror(scenario_a_sell()), make_config())
    return s


# ---------------- sound ----------------


class FakePlayer:
    def __init__(self) -> None:
        self.played: list[Path] = []
        self.beeps = 0
        self.fail = False
        self.block: threading.Event | None = None
        self.started = threading.Event()

    def play_file(self, path: Path) -> None:
        self.started.set()
        if self.block is not None:
            self.block.wait(5)
        if self.fail:
            raise RuntimeError("audio device busy")
        self.played.append(path)

    def beep(self) -> None:
        self.beeps += 1


def sync(job: Any) -> None:
    job()


def test_buy_and_sell_use_different_configured_files(tmp_path: Path) -> None:
    (tmp_path / "up.wav").write_bytes(b"x")
    (tmp_path / "down.wav").write_bytes(b"x")
    player = FakePlayer()
    notifier = SoundNotifier(
        SoundConfig(buy_sound="up.wav", sell_sound="down.wav"), tmp_path, player, runner=sync
    )
    notifier.notify(buy())
    notifier.notify(sell())
    assert player.played == [tmp_path / "up.wav", tmp_path / "down.wav"]


def test_default_sound_files_exist_and_are_distinct() -> None:
    cfg = SoundConfig()
    buy_file, sell_file = REPO / cfg.buy_sound, REPO / cfg.sell_sound
    assert buy_file.is_file() and sell_file.is_file()
    assert buy_file.read_bytes() != sell_file.read_bytes()
    player = FakePlayer()
    SoundNotifier(cfg, REPO, player, runner=sync).notify(buy())
    assert player.played == [buy_file]


def test_disabled_sound_is_silent_but_test_play_still_works(tmp_path: Path) -> None:
    (tmp_path / "a.wav").write_bytes(b"x")
    player = FakePlayer()
    notifier = SoundNotifier(
        SoundConfig(enabled=False, buy_sound="a.wav", sell_sound="a.wav"),
        tmp_path,
        player,
        runner=sync,
    )
    notifier.notify(sell())
    assert player.played == [] and not notifier.enabled
    notifier.test_play(Direction.SELL)
    assert player.played == [tmp_path / "a.wav"]


def test_runtime_reconfiguration(tmp_path: Path) -> None:
    (tmp_path / "x.wav").write_bytes(b"x")
    player = FakePlayer()
    notifier = SoundNotifier(SoundConfig(enabled=False), tmp_path, player, runner=sync)
    notifier.configure(SoundConfig(enabled=True, sell_sound="x.wav"))
    notifier.notify(sell())
    assert player.played == [tmp_path / "x.wav"]


def test_absolute_path_is_used_as_is(tmp_path: Path) -> None:
    f = tmp_path / "abs.wav"
    f.write_bytes(b"x")
    notifier = SoundNotifier(SoundConfig(sell_sound=str(f)), Path("/elsewhere"), FakePlayer())
    assert notifier.sound_path(Direction.SELL) == f


def test_missing_file_falls_back_to_beep(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    player = FakePlayer()
    SoundNotifier(SoundConfig(sell_sound="nope.wav"), tmp_path, player, runner=sync).notify(sell())
    assert player.beeps == 1 and player.played == []
    assert "not found" in caplog.text


def test_player_failure_is_contained(tmp_path: Path) -> None:
    (tmp_path / "a.wav").write_bytes(b"x")
    player = FakePlayer()
    player.fail = True
    SoundNotifier(SoundConfig(sell_sound="a.wav"), tmp_path, player, runner=sync).notify(sell())


def test_sound_does_not_block_the_caller(tmp_path: Path) -> None:
    (tmp_path / "a.wav").write_bytes(b"x")
    player = FakePlayer()
    player.block = threading.Event()  # playback "takes forever"
    notifier = SoundNotifier(
        SoundConfig(sell_sound="a.wav"), tmp_path, player
    )  # real thread runner
    returned = threading.Event()

    def call() -> None:
        notifier.notify(sell())
        returned.set()

    threading.Thread(target=call, daemon=True).start()
    assert returned.wait(1.0), "notify() blocked the monitoring loop"
    assert player.started.wait(1.0)
    player.block.set()


# ---------------- dispatcher ----------------


def test_dispatcher_isolates_failures_and_keeps_order() -> None:
    calls: list[str] = []

    class Good:
        def __init__(self, name: str) -> None:
            self.name = name

        def notify(self, signal: Signal) -> None:
            calls.append(self.name)

    class Bad:
        def notify(self, signal: Signal) -> None:
            raise RuntimeError("boom")

    NotificationDispatcher([Good("sound"), Bad(), Good("telegram")]).dispatch(sell())
    assert calls == ["sound", "telegram"]


# ---------------- secrets ----------------


def test_secrets_repr_hides_token() -> None:
    secrets = TelegramSecrets(TOKEN, "42")
    assert TOKEN not in repr(secrets) and TOKEN not in str(secrets) and "42" in repr(secrets)


def test_file_store_round_trip_and_permissions(tmp_path: Path) -> None:
    store = FileSecretStore(tmp_path / "secrets.toml")
    assert store.load() is None
    store.save(TelegramSecrets(TOKEN, "-1001234"))
    loaded = store.load()
    assert loaded is not None and loaded.bot_token == TOKEN and loaded.chat_id == "-1001234"
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / "secrets.toml").stat().st_mode) == 0o600
    store.clear()
    assert store.load() is None


def test_file_store_handles_awkward_characters_and_corruption(tmp_path: Path) -> None:
    path = tmp_path / "s.toml"
    store = FileSecretStore(path)
    store.save(TelegramSecrets('to"ken\\x', "@chan"))
    loaded = store.load()
    assert loaded is not None and loaded.bot_token == 'to"ken\\x'
    path.write_text("not [valid toml")
    assert store.load() is None
    path.write_text('[telegram]\nbot_token = 5\nchat_id = "1"\n')
    assert store.load() is None


class FakeKeyring:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}
        self.broken = False

    def get_password(self, service: str, name: str) -> str | None:
        if self.broken:
            raise RuntimeError("no keyring backend")
        return self.data.get((service, name))

    def set_password(self, service: str, name: str, value: str) -> None:
        if self.broken:
            raise RuntimeError("no keyring backend")
        self.data[(service, name)] = value

    def delete_password(self, service: str, name: str) -> None:
        del self.data[(service, name)]


def test_keyring_store_and_chained_fallback(tmp_path: Path) -> None:
    keyring = FakeKeyring()
    file_store = FileSecretStore(tmp_path / "secrets.toml")
    chained = ChainedSecretStore(KeyringSecretStore(keyring), file_store)
    chained.save(TelegramSecrets(TOKEN, "7"))
    assert (tmp_path / "secrets.toml").exists() is False  # no plaintext copy when keyring works
    assert chained.load() == TelegramSecrets(TOKEN, "7")

    keyring.broken = True  # e.g. headless machine without a keyring backend
    chained.save(TelegramSecrets("tok-2", "8"))
    loaded = chained.load()
    assert loaded is not None and loaded.chat_id == "8"
    assert (tmp_path / "secrets.toml").exists()


def test_credential_validation() -> None:
    assert validate_telegram_credentials(TOKEN, "123456789") == []
    assert validate_telegram_credentials(TOKEN, "-1001234567890") == []
    assert validate_telegram_credentials(TOKEN, "@my_channel") == []
    assert len(validate_telegram_credentials("nope", "abc")) == 2


def test_no_credentials_in_repo_config_or_source() -> None:
    """Credentials must never be hardcoded: scan config and source for token-shaped strings."""
    import re

    pattern = re.compile(r"\d{8,}:[A-Za-z0-9_-]{30,}")
    for path in [*REPO.joinpath("src").rglob("*.py"), *REPO.joinpath("config").glob("*.toml")]:
        assert not pattern.search(path.read_text()), f"token-like string in {path}"
    assert "bot_token" not in (REPO / "config" / "config.example.toml").read_text()


# ---------------- logging ----------------


def test_formatter_redacts_tokens_secrets_and_tracebacks() -> None:
    fmt = RedactingFormatter("%(message)s")
    fmt.add_secret("my-chat-secret")
    try:
        raise RuntimeError(f"https://api.telegram.org/bot{TOKEN}/sendMessage failed")
    except RuntimeError:
        import sys

        record = logging.LogRecord(
            "x", logging.ERROR, "f", 1, "token=%s chat=my-chat-secret", (TOKEN,), sys.exc_info()
        )
    out = fmt.format(record)
    assert TOKEN not in out and "my-chat-secret" not in out and "bot<token>" in out


def test_setup_logging_writes_file_without_secrets(tmp_path: Path) -> None:
    log_file = setup_logging(tmp_path / "logs", console=False)
    register_secret("super-secret-chat-id")
    logging.getLogger("xauassist.test").error("leak? %s and %s", TOKEN, "super-secret-chat-id")
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = log_file.read_text()
    assert "leak?" in text and TOKEN not in text and "super-secret-chat-id" not in text
