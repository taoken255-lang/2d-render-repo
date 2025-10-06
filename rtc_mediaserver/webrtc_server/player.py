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

TIME_SPAN = 0.02

class PlayerStreamTrack(MediaStreamTrack):
    """Custom aiortc track that pulls frames from an internal queue."""

    kind: str

    def __init__(self, player: "WebRTCMediaPlayer", kind: str):
        super().__init__()
        self.kind = kind
        self._player = player
        self._count = 0
        self._last_sent_time = 0
        # Диагностика частоты recv()
        self._recv_count = 0
        self._last_recv_time = 0
        self._recv_times = []
        # FIX сбалансированные размеры очередей для предотвращения рассинхронизации  
        # Видео: 5 кадров * 40ms = 200ms буфер
        # Аудио: 10 чанков * 20ms = 200ms буфер (тот же буфер по времени!)
        self._queue: asyncio.Queue[Tuple[Union[Frame, Packet], float]] = asyncio.Queue()  # FIX равные буферы по времени
        self._tb = VIDEO_TB if kind == "video" else AUDIO_SETTINGS.audio_tb
        self._period = VIDEO_PTIME if kind == "video" else AUDIO_SETTINGS.audio_ptime
        self._rate = VIDEO_CLOCK if kind == "video" else AUDIO_SETTINGS.sample_rate
        self._pts: int = 0
        self._start: Optional[float] = None  # in perf_counter timebase (not wallclock)  # FIX ясная семантика базы
        self._t0 = time.time()

    async def _sleep_until_slot(self) -> None:
        """Sleep just enough to achieve a constant frame/packet rate."""

        t0 = self._player._ensure_t0()
        if self._start is None:
            self._start = t0
            return

        self._pts += int(self._rate * self._period)
        target = self._start + self._pts / self._rate
        now = time.perf_counter()  # FIX perf_counter вместо time.time()
        delay = target - now

        if self._recv_count % 50 == 0:
            expected_time = self._start + (self._recv_count * self._period)
            drift = now - expected_time
            logger.info(f"🕐 {self.kind} SLOT: target={target:.6f} now={now:.6f} delay={delay*1000:.2f}ms "
                      f"drift={drift*1000:.2f}ms pts={self._pts} recv#{self._recv_count}")
        
        if delay > 0:
            await asyncio.sleep(delay)
            if delay > 0.05 and self._recv_count % 10 == 0:  # >50ms 
                logger.warning(f"{self.kind} LONG SLEEP: {delay*1000:.1f}ms")
        else:
            # FIX мягкая ресинхронизация, если сильно опоздали (например, >120 мс):
            # подтягиваем базу, чтобы не копить постоянное отставание
            if delay < -0.12:
                old_start = self._start
                self._start = now - self._pts / self._rate  # FIX soft resync

    async def recv(self):  # type: ignore[override]
        import time
        recv_start = time.perf_counter()
        
        # Диагностика частоты recv()
        self._recv_count += 1
        if self._last_recv_time > 0:
            interval = recv_start - self._last_recv_time
            self._recv_times.append(interval)
            # Хранить только последние 20 интервалов
            if len(self._recv_times) > 20:
                self._recv_times.pop(0)
        self._last_recv_time = recv_start
        
        self._player._ensure_worker(self)

        # FIX КРИТИЧНО: восстанавливаем синхронизацию!
        sleep_start = time.perf_counter()
        await self._sleep_until_slot()
        sleep_duration = time.perf_counter() - sleep_start
        
        # 🔍 Проверяем состояние очереди
        queue_size = self._queue.qsize()

        frame, _ = await self._queue.get()

        # FIX КРИТИЧНО: восстанавливаем правильные PTS!
        frame.pts = self._pts
        frame.time_base = self._tb

        # 🔍 ДЕТАЛЬНАЯ ДИАГНОСТИКА каждые 25 вызовов
        if self._recv_count % 25 == 0:
            if self._recv_times:
                avg_interval = sum(self._recv_times) / len(self._recv_times)
                freq = 1.0 / avg_interval if avg_interval > 0 else 0
                expected_freq = 50 if self.kind == "audio" else 25
                min_interval = min(self._recv_times) * 1000
                max_interval = max(self._recv_times) * 1000
                
                logger.info(f"{self.kind} TIMING: freq={freq:.1f}Hz (exp:{expected_freq}) "
                          f"interval={avg_interval*1000:.1f}ms (min:{min_interval:.1f} max:{max_interval:.1f}) "
                          f"sleep={sleep_duration*1000:.2f}ms queue={queue_size} pts={self._pts}")

                if freq > expected_freq * 1.2:
                    logger.warning(f"{self.kind} FREQ TOO HIGH: {freq:.1f}Hz > {expected_freq*1.2:.1f}Hz")
                elif freq < expected_freq * 0.8:
                    logger.warning(f"{self.kind} FREQ TOO LOW: {freq:.1f}Hz < {expected_freq*0.8:.1f}Hz")
                    
                if sleep_duration < 0.001:  # <1ms sleep
                    logger.warning(f"{self.kind} NO SLEEP: aiortc ignoring our timing! sleep={sleep_duration*1000:.2f}ms")
                elif sleep_duration > 0.1:  # >100ms sleep
                    logger.warning(f"{self.kind} EXCESSIVE SLEEP: {sleep_duration*1000:.1f}ms")

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

        self._last_batch_time = 0
        self._audio_tb = AUDIO_SETTINGS.audio_tb
        self._video_tb = VIDEO_TB
        self._audio_pts = 0
        self._video_pts = 0
        self._t0 = time.time()

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
            now = time.perf_counter()

            if now >= next_audio:
                self._push_audio()
                missed = int((now - next_audio) / AUDIO_DT)
                next_audio += (missed + 1) * AUDIO_DT

            if now >= next_video:
                self._push_video()
                missed = int((now - next_video) / VIDEO_DT)
                next_video += (missed + 1) * VIDEO_DT

            sleep = min(next_audio, next_video) - now
            if sleep > 0:
                time.sleep(sleep)
            else:
                time.sleep(0.001)

    # ───────────────────── Internal helpers ────────────────────────────
    def _load_next_batch(self) -> bool:
        """Pop next synced batch (1 sec audio + 25 frames) from queue."""
        from rtc_mediaserver.logging_config import get_logger

        if SYNC_QUEUE.empty():
            return False

        audio_sec, frames25 = SYNC_QUEUE.get()

        # FIX КРИТИЧНО: правильное разбиение на чанки!
        # Нарезаем 1 сек аудио на 50 чанков по 20мс
        for i in range(0, len(audio_sec), AUDIO_SETTINGS.audio_samples):
            self._audio_chunks.append(audio_sec[i:i + AUDIO_SETTINGS.audio_samples])

        # Добавляем 25 видео кадров
        self._video_frames.extend(frames25)

        get_logger(__name__).info(
            f"Loaded synced batch: {len(self._audio_chunks)} audio chunks, {len(self._video_frames)} video frames"
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
                self._loop.call_soon_threadsafe(
                    self._audio_track._queue.put_nowait,
                    (frame, time.perf_counter())
                )
            except asyncio.QueueFull:
                logger.warning("push_audio: queue full, dropping 20ms chunk")

    def _push_video(self) -> None:
        if not self._video_frames:
            if not self._load_next_batch():
                logger.debug("push_video: no frames available")
                return
        if not self._video_frames:
            logger.debug("push_video: no frames available after batch load")
            return
            
        # FIX проверка синхронизации буферов
        audio_count = len(self._audio_chunks)
        video_count = len(self._video_frames)
        if audio_count == 0 and video_count > 10:
            logger.warning("🚨 DESYNC: Video buffer has %d frames but audio buffer empty!", video_count)
        elif video_count == 0 and audio_count > 20:
            logger.warning("🚨 DESYNC: Audio buffer has %d chunks but video buffer empty!", audio_count)
            
        arr = self._video_frames.popleft()
        frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
        if self._loop:
            try:
                # FIX неблокирующая доставка; при переполнении — дроп кадра (видео догонит само)
                self._loop.call_soon_threadsafe(
                    self._video_track._queue.put_nowait,
                    (frame, time.perf_counter()),
                )
            except asyncio.QueueFull:
                logger.warning("push_video: queue full, dropping frame (A/V desync possible!)")
