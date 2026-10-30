"""Local sound alerts: different sounds for BUY and SELL, played off the monitoring thread."""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from xauassist.config.models import SoundConfig
from xauassist.domain.enums import Direction
from xauassist.domain.signal import Signal

log = logging.getLogger(__name__)


class SoundPlayer(Protocol):
    def play_file(self, path: Path) -> None:
        """Start playback of an audio file (may return immediately or block)."""
        ...

    def beep(self) -> None:
        """Fallback alert when the configured file cannot be played."""
        ...


class WinsoundPlayer:
    """Windows: winsound plays WAV files asynchronously."""

    def __init__(self) -> None:
        import winsound  # type: ignore[import-not-found,unused-ignore]

        self._ws: Any = winsound

    def play_file(self, path: Path) -> None:
        self._ws.PlaySound(str(path), self._ws.SND_FILENAME | self._ws.SND_ASYNC)

    def beep(self) -> None:
        self._ws.MessageBeep()


class SubprocessPlayer:
    """macOS/Linux development fallback using an installed command-line player."""

    def __init__(self, command: list[str]) -> None:
        self._command = command

    def play_file(self, path: Path) -> None:
        subprocess.Popen(  # noqa: S603 - fixed player command, path is a local file
            [*self._command, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    def beep(self) -> None:
        sys.stdout.write("\a")
        sys.stdout.flush()


class NullPlayer:
    def play_file(self, path: Path) -> None:
        log.warning("No audio backend available; cannot play %s", path)

    def beep(self) -> None:
        log.warning("No audio backend available; cannot beep")


def create_default_player() -> SoundPlayer:
    if sys.platform == "win32":
        try:
            return WinsoundPlayer()
        except ImportError:
            return NullPlayer()
    for command in (["afplay"], ["paplay"], ["aplay", "-q"]):
        if shutil.which(command[0]):
            return SubprocessPlayer(command)
    return NullPlayer()


def _run_in_thread(job: Callable[[], None]) -> None:
    threading.Thread(target=job, name="sound-player", daemon=True).start()


class SoundNotifier:
    def __init__(
        self,
        config: SoundConfig,
        base_dir: Path,
        player: SoundPlayer,
        *,
        runner: Callable[[Callable[[], None]], None] = _run_in_thread,
    ) -> None:
        self._cfg = config
        self._base_dir = base_dir
        self._player = player
        self._runner = runner

    @property
    def enabled(self) -> bool:
        return self._cfg.enabled

    def configure(self, config: SoundConfig) -> None:
        """Apply new settings (e.g. from the Settings dialog) without restarting."""
        self._cfg = config

    def notify(self, signal: Signal) -> None:
        if self._cfg.enabled:
            self._runner(lambda: self._play(signal.direction))

    def test_play(self, direction: Direction) -> None:
        """Play the sound regardless of the enabled flag (Settings 'Test' buttons)."""
        self._runner(lambda: self._play(direction))

    def sound_path(self, direction: Direction) -> Path:
        name = self._cfg.buy_sound if direction is Direction.BUY else self._cfg.sell_sound
        path = Path(name)
        return path if path.is_absolute() else self._base_dir / path

    def _play(self, direction: Direction) -> None:
        path = self.sound_path(direction)
        try:
            if path.is_file():
                self._player.play_file(path)
            else:
                log.warning("Sound file not found: %s; using a beep instead", path)
                self._player.beep()
        except Exception:
            log.exception("Sound playback failed for %s", path)
