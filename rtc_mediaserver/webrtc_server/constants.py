"""Constants for WebRTC media server components."""
from __future__ import annotations

import asyncio
import fractions
import os
from pathlib import Path

__all__ = [
    "AUDIO_SETTINGS",
    "VIDEO_CLOCK",
    "VIDEO_PTIME",
    "VIDEO_TB",
    "BASE_DIR",
    "AUDIO_FILE",
    "FRAMES_DIR",
    "DEFAULT_IMAGE_PATH",
    "AUDIO_CHUNK_MS",
    "FRAMES_PER_CHUNK",
]


class AudioParams:
    """Container for audio-related parameters that may change at runtime."""

    def __init__(self) -> None:
        # Will be updated once WAV header is parsed on upload
        self.sample_rate: int = 16000

    # 20 ms – fixed packetization time expected by the client
    @property
    def audio_ptime(self) -> float:
        return 0.020

    @property
    def audio_samples(self) -> int:
        """Number of PCM samples in a single 20 ms chunk."""
        return int(self.audio_ptime * self.sample_rate) if self.sample_rate else 0

    @property
    def samples_per_chunk(self) -> int:
        """Samples corresponding to configured FRAMES_PER_CHUNK."""
        duration = AUDIO_CHUNK_MS / 1000.0  # seconds
        return int(duration * self.sample_rate)

    @property
    def audio_tb(self) -> fractions.Fraction:  # type: ignore[override]
        """Time-base for audio frames."""
        return fractions.Fraction(1, self.sample_rate) if self.sample_rate else fractions.Fraction(0, 1)


AUDIO_SETTINGS = AudioParams()

# ── New chunk settings based on audio duration ──────────────────────
# Duration of minimal audio chunk required by gRPC (milliseconds)
AUDIO_CHUNK_MS: int = int(os.getenv("AUDIO_CHUNK_MS", "600"))  # 0.6 seconds
# -------------------------------------------------------------------

# Video constants
VIDEO_CLOCK = 90_000          # RTP 90 kHz clock
VIDEO_PTIME = 0.040           # 25 fps → 40 ms
VIDEO_TB = fractions.Fraction(1, VIDEO_CLOCK)

# Frames per chunk derived from video packetization time (40 ms → 25 fps)
FRAMES_PER_CHUNK: int = int(round(AUDIO_CHUNK_MS / (VIDEO_PTIME * 1000)))  # e.g., 600/40 = 15

# Filesystem paths
BASE_DIR = Path(__file__).resolve().parent.parent
AUDIO_FILE = BASE_DIR / "sample.wav"
FRAMES_DIR = BASE_DIR / "mock" / "images"
DEFAULT_IMAGE_PATH = str((BASE_DIR.parent / os.getenv("DEFAULT_IMAGE", "img3.png")).resolve())

CAN_SEND_FRAMES = asyncio.Event()
AVATAR_SET = asyncio.Event()
INIT_DONE = asyncio.Event()

WS_CONTROL_CONNECTED = asyncio.Event()
RTC_STREAM_CONNECTED = asyncio.Event()

CLIENT_COMMANDS = asyncio.Queue()
USER_EVENTS = asyncio.Queue()

INTERRUPT_CALLED = asyncio.Event()
ANIMATION_CALLED = asyncio.Event()
EMOTION_CALLED = asyncio.Event()
