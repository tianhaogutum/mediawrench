"""Deterministic, original synthetic PCM audio; no downloads or copyrighted media."""
from array import array
import math
from pathlib import Path
import sys
import wave


def write_tone(path: Path, seconds: float = 12.0, *, kind: str = "modulated",
               channels: int = 1, rate: int = 48000) -> None:
    samples = array("h")
    for index in range(round(seconds * rate)):
        time = index / rate
        if kind == "silence":
            signal = 0.0
        elif kind == "clipped":
            signal = max(-1, min(1, 1.6 * math.sin(2 * math.pi * 440 * time)))
        elif kind == "dynamic":
            amplitude = 0.008 if int(time // 6) % 2 == 0 else 0.45
            signal = amplitude * math.sin(2 * math.pi * 440 * time)
        else:
            amplitude = 0.065 * (0.65 + 0.35 * math.sin(2 * math.pi * 0.23 * time))
            signal = amplitude * (math.sin(2 * math.pi * 440 * time) +
                                  0.15 * math.sin(2 * math.pi * 880 * time))
        sample = max(-32768, min(32767, round(signal * 32767)))
        samples.extend([sample] * channels)
    if sys.byteorder != "little":
        samples.byteswap()
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(samples.tobytes())
