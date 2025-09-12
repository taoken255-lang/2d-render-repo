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
from .shared import SYNC_QUEUE, SYNC_QUEUE_SEM

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
        # FIX ограничил размер очереди, чтобы не накапливать лаг и не блокироваться
        # (видео можно дропать, аудио — иногда тоже лучше дропнуть, чем уехать во времени)
        self._queue: asyncio.Queue[Tuple[Union[Frame, Packet], float]] = asyncio.Queue(maxsize=5)  # FIX bounded queue
        self._tb = VIDEO_TB if kind == "video" else AUDIO_SETTINGS.audio_tb
        self._period = VIDEO_PTIME if kind == "video" else AUDIO_SETTINGS.audio_ptime
        self._rate = VIDEO_CLOCK if kind == "video" else AUDIO_SETTINGS.sample_rate
        self._pts: int = 0
        self._start: Optional[float] = None  # in perf_counter timebase (not wallclock)  # FIX ясная семантика базы

    async def _sleep_until_slot(self) -> None:
        """Sleep just enough to achieve a constant frame/packet rate."""
        # FIX используем единый мастер-час от плеера + perf_counter для точного монотонного тайминга
        t0 = self._player._ensure_t0()  # FIX общий старт для аудио и видео
        if self._start is None:
            # FIX вместо time.time() стартуем от общего t0 (perf_counter)
            self._start = t0
            return

        self._pts += int(self._rate * self._period)
        target = self._start + self._pts / self._rate
        now = time.perf_counter()  # FIX perf_counter вместо time.time()
        delay = target - now
        if delay > 0:
            await asyncio.sleep(delay)
        else:
            # FIX мягкая ресинхронизация, если сильно опоздали (например, >120 мс):
            # подтягиваем базу, чтобы не копить постоянное отставание
            if delay < -0.12:
                self._start = now - self._pts / self._rate  # FIX soft resync

    async def recv(self):  # type: ignore[override]
        self._player._ensure_worker(self)
        frame, _ = await self._queue.get()
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

        # FIX единый мастер-час для обоих треков, защищённый локом
        self._t0_perf: Optional[float] = None  # perf_counter timestamp  # FIX master clock storage
        self._t0_lock = threading.Lock()  # FIX guard for t0 init

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

    # FIX метод для инициализации общего t0 на perf_counter
    def _ensure_t0(self) -> float:
        with self._t0_lock:
            if self._t0_perf is None:
                self._t0_perf = time.perf_counter()
            return self._t0_perf

    # ───────────────────── Background worker ───────────────────────────
    def _worker(self) -> None:
        # FIX перешли на расписание по дедлайнам (каждые 20 мс для аудио и 40 мс для видео)
        # вместо loop_idx%2 — устойчиво к долгим итерациям и системным скачкам.
        AUDIO_DT = AUDIO_SETTINGS.audio_ptime  # 0.02
        VIDEO_DT = VIDEO_PTIME                # 0.04

        base = self._ensure_t0()  # FIX выравниваем дедлайны по общему t0
        next_audio = base
        next_video = base

        while not self._quit.is_set():
            now = time.perf_counter()  # FIX perf_counter для монотонного времени

            if now >= next_audio:
                self._push_audio()
                # FIX если сильно опоздали, перескакиваем столько шагов, сколько пропустили
                missed = int((now - next_audio) / AUDIO_DT)
                next_audio += (missed + 1) * AUDIO_DT

            if now >= next_video:
                self._push_video()
                # FIX видео безопаснее дропать — тоже перескакиваем пропущенные слоты
                missed = int((now - next_video) / VIDEO_DT)
                next_video += (missed + 1) * VIDEO_DT

            sleep = min(next_audio, next_video) - now
            if sleep > 0:
                time.sleep(sleep)
            else:
                # FIX короткий сон, чтобы избежать busy-loop при микролагах
                time.sleep(0.001)

    # ───────────────────── Internal helpers ────────────────────────────
    def _load_next_batch(self) -> bool:
        """Pop next synced batch (1 sec audio + 25 frames) from queue."""
        from rtc_mediaserver.logging_config import get_logger  # avoid circular
        if SYNC_QUEUE.empty():
            return False
        audio_sec, frames25 = SYNC_QUEUE.get()
        # FIX освобождаем семафор из треда через call_soon_threadsafe корректно
        if self._loop:
            self._loop.call_soon_threadsafe(SYNC_QUEUE_SEM.release)  # FIX thread-safe release
        else:
            # fallback (теоретически не должен понадобиться)
            SYNC_QUEUE_SEM.release()
        # Slice audio into 20-ms chunks
        for i in range(0, len(audio_sec), AUDIO_SETTINGS.audio_samples):
            self._audio_chunks.append(audio_sec[i:i + AUDIO_SETTINGS.audio_samples])
        self._video_frames.extend(frames25)
        get_logger(__name__).info(
            "Loaded synced batch into player (chunks=%d frames=%d)",
            len(self._audio_chunks), len(self._video_frames)
        )
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
            try:
                # FIX неблокирующая доставка кадра в очередь трека; при переполнении — дроп
                self._loop.call_soon_threadsafe(
                    self._audio_track._queue.put_nowait,
                    (frame, time.perf_counter()),  # FIX perf_counter timestamp
                )
            except asyncio.QueueFull:
                # FIX лучше дропнуть 20 мс, чем накапливать дрейф
                logger.debug("push_audio: queue full, dropping 20ms chunk")

    def _push_video(self) -> None:
        ts = time.perf_counter()  # FIX perf_counter вместо time.time()
        if not self._video_frames:
            self._load_next_batch()
        if not self._video_frames:
            logger.debug("push_video: no frames available")
            return
        arr = self._video_frames.popleft()
        frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
        if self._loop:
            try:
                # FIX неблокирующая доставка; при переполнении — дроп кадра (видео догонит само)
                self._loop.call_soon_threadsafe(
                    self._video_track._queue.put_nowait,
                    (frame, ts),
                )
            except asyncio.QueueFull:
                logger.debug("push_video: queue full, dropping frame")
