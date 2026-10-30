"""Generate the default alert sounds (assets/sounds/buy.wav and sell.wav).

BUY  = rising two-tone chime;  SELL = falling two-tone chime. Replace the files (or point the
[sound] config at your own WAVs) to customise.
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

RATE = 44100


def tone(freq: float, seconds: float, volume: float = 0.5) -> list[int]:
    n = int(RATE * seconds)
    out = []
    for i in range(n):
        envelope = min(1.0, i / (0.01 * RATE), (n - i) / (0.05 * RATE))  # soft attack/release
        out.append(int(32767 * volume * envelope * math.sin(2 * math.pi * freq * i / RATE)))
    return out


def write(path: Path, freqs: list[float]) -> None:
    samples: list[int] = []
    for f in freqs:
        samples += tone(f, 0.28)
        samples += [0] * int(RATE * 0.04)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))


if __name__ == "__main__":
    out = Path(__file__).resolve().parents[1] / "assets" / "sounds"
    out.mkdir(parents=True, exist_ok=True)
    write(out / "buy.wav", [660.0, 880.0, 1100.0])
    write(out / "sell.wav", [880.0, 660.0, 440.0])
    print(f"wrote {out}/buy.wav and sell.wav")
