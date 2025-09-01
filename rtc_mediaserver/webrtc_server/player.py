"""Media player implementation that feeds audio & video from queues to aiortc tracks."""
from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from typing import Deque, Optional, Set, Tuple, Union

import av  # type: ignore
import numpy as np  # type: ignore
from aiortc import MediaStreamTrack  # type: ignore
from av.frame import Frame  # type: ignore
from av.packet import Packet  # type: ignore

from rtc_mediaserver.logging_config import get_logger, setup_default_logging
from .constants import AUDIO_SETTINGS, VIDEO_CLOCK, VIDEO_PTIME, VIDEO_TB
from .shared import SYNC_QUEUE

# Make sure logging is configured as early as possible
setup_default_logging()
logger = get_logger(__name__)

__all__ = [
    "PlayerStreamTrack",
    "WebRTCMediaPlayer",
]


class PlayerStreamTrack(MediaStreamTrack):
    """Custom aiortc track that pulls frames from an internal queue."""

    kind: str

    def __init__(self, player: "WebRTCMediaPlayer", kind: str):
        super().__init__()
        self.kind = kind
        self._player = player
        self._queue: asyncio.Queue[Tuple[Union[Frame, Packet], float]] = asyncio.Queue()
        self._tb = VIDEO_TB if kind == "video" else AUDIO_SETTINGS.audio_tb
        self._period = VIDEO_PTIME if kind == "video" else AUDIO_SETTINGS.audio_ptime
        self._rate = VIDEO_CLOCK if kind == "video" else AUDIO_SETTINGS.sample_rate
        self._pts: int = 0
        self._start: Optional[float] = None

    async def _sleep_until_slot(self) -> None:
        """Sleep just enough to achieve a constant frame/packet rate."""
        if self._start is None:
            # Remember when streaming started
            self._start = time.time()
            return

        self._pts += int(self._rate * self._period)
        target = self._start + self._pts / self._rate
        delay = target - time.time()
        if delay > 0:
            await asyncio.sleep(delay)

    async def recv(self):  # type: ignore[override]
        self._player._ensure_worker(self)
        frame, _timestamp = await self._queue.get()
        await self._sleep_until_slot()
        frame.pts = self._pts
        frame.time_base = self._tb
        return frame

    def stop(self) -> None:  # type: ignore[override]
        super().stop()
        if self._player:
            self._player._track_stopped(self)
            self._player = None


class WebRTCMediaPlayer:
    """Background thread that converts synced audio/video batches into aiortc frames."""

    def __init__(self) -> None:
        self._audio_track = PlayerStreamTrack(self, "audio")
        self._video_track = PlayerStreamTrack(self, "video")
        self._active: Set[PlayerStreamTrack] = set()
        self._thread: Optional[threading.Thread] = None
        self._quit = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Buffers for in-flight batch currently being streamed
        self._audio_chunks: Deque[np.ndarray] = deque()
        self._video_frames: Deque[np.ndarray] = deque()

    # Public tracks exposed to aiortc peer connection
    @property
    def audio(self) -> PlayerStreamTrack:  # type: ignore[override]
        return self._audio_track

    @property
    def video(self) -> PlayerStreamTrack:  # type: ignore[override]
        return self._video_track

    # ───────────────── Track/worker lifecycle helpers ──────────────────
    def _ensure_worker(self, track: PlayerStreamTrack) -> None:
        """Start background worker the first time any track is pulled."""
        self._active.add(track)
        if self._thread is None:
            self._loop = asyncio.get_running_loop()
            self._quit.clear()
            self._thread = threading.Thread(target=self._worker, name="media-player", daemon=True)
            self._thread.start()
            logger.info("media thread started")

    def _track_stopped(self, track: PlayerStreamTrack) -> None:
        self._active.discard(track)
        if not self._active and self._thread:
            self._quit.set()
            self._thread.join()
            self._thread = None
            logger.info("media thread stopped")

    # ───────────────────── Background worker ───────────────────────────
    def _worker(self) -> None:
        loop_idx = 0  # используем для дискретизации 20 мс
        while not self._quit.is_set():
            start = time.monotonic()

            # 20-мс аудио-чанк на каждой итерации
            self._push_audio()

            # Видео нужно 25 fps ⇒ кадр каждые 40 мс ⇒ через итерацию
            if loop_idx % 2 == 0:
                self._push_video()

            loop_idx += 1

            # сон = 20 мс – (время выполнения)
            sleep = AUDIO_SETTINGS.audio_ptime - (time.monotonic() - start)
            if sleep > 0:
                time.sleep(sleep)

    # ───────────────────── Internal helpers ────────────────────────────
    def _load_next_batch(self) -> bool:
        """Pop next synced batch (1 sec audio + 25 frames) from queue."""
        from rtc_mediaserver.logging_config import get_logger  # avoid circular
        if SYNC_QUEUE.empty():
            return False
        audio_sec, frames25 = SYNC_QUEUE.get()
        # Slice audio into 20-ms chunks
        for i in range(0, len(audio_sec), AUDIO_SETTINGS.audio_samples):
            self._audio_chunks.append(audio_sec[i:i + AUDIO_SETTINGS.audio_samples])
        self._video_frames.extend(frames25)
        get_logger(__name__).info("Loaded synced batch into player (chunks=%d frames=%d)",
                                   len(self._audio_chunks), len(self._video_frames))
        return True

    def _push_audio(self) -> None:
        if not self._audio_chunks:
            self._load_next_batch()
        if not self._audio_chunks:
            logger.debug("push_audio: no chunks available")
            return
        chunk = self._audio_chunks.popleft()
        frame = av.AudioFrame(format="s16", layout="mono", samples=AUDIO_SETTINGS.audio_samples)
        frame.planes[0].update(chunk.tobytes())
        frame.sample_rate = AUDIO_SETTINGS.sample_rate
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._audio_track._queue.put((frame, time.time())), self._loop)
            #logger.info("push_audio queued %d samples (remaining=%d)", AUDIO_SETTINGS.audio_samples, len(self._audio_chunks))

    def _push_video(self) -> None:
        ts = time.time()
        if not self._video_frames:
            self._load_next_batch()
        if not self._video_frames:
            logger.debug("push_video: no frames available")
            return
        arr = self._video_frames.popleft()
        frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._video_track._queue.put((frame, ts)), self._loop)
            #logger.info("push_video queued frame (remaining=%d)", len(self._video_frames))