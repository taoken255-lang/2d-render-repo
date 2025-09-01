"""gRPC client that streams audio to render service and receives corresponding frames."""
from __future__ import annotations

import asyncio
import glob
import logging
import re
import wave
from pathlib import Path
from typing import Deque, List, Tuple

import numpy as np  # type: ignore
from PIL import Image

from rtc_mediaserver.logging_config import get_logger, setup_default_logging
from .constants import DEFAULT_IMAGE_PATH, AUDIO_SETTINGS, CAN_SEND_FRAMES, \
    FRAMES_PER_CHUNK, USER_EVENTS, INTERRUPT_CALLED, ANIMATION_CALLED, EMOTION_CALLED
from .shared import AUDIO_SECOND_QUEUE, SYNC_QUEUE
from ..config import settings
from ..events import ServiceEvents, Conditions

# Configure logging early
setup_default_logging()
logger = get_logger(__name__)

import grpc  # type: ignore
from grpc import aio # type: ignore
from rtc_mediaserver.proto import render_service_pb2_grpc, render_service_pb2  # type: ignore

async def stream_worker_aio() -> None:
    """Background task that maintains bidirectional RenderStream gRPC call."""
    logger.info("Starting gRPC connection")
    if grpc is None:
        logger.error("grpc library not available – stream worker aborted")
        return

    opts: List[Tuple[str, int]] = [
        ("grpc.max_receive_message_length", 10 * 1024 * 1024),
        ("grpc.max_send_message_length", 10 * 1024 * 1024),
    ]

    if settings.grpc_secure_channel:
        creds = grpc.ssl_channel_credentials()
        channel = aio.secure_channel(settings.grpc_server_url, credentials=creds, options=opts)
    else:
        channel = aio.insecure_channel(settings.grpc_server_url, options=opts)

    stub = render_service_pb2_grpc.RenderServiceStub(channel)
    logger.info(f"gRPC aio stream opened → {settings.grpc_server_url}")

    # Keep state between iterations
    from collections import deque
    pending_audio: Deque[Tuple[np.ndarray, int, ServiceEvents | None]] = deque()
    frames_batch: List[np.ndarray] = []

    CHUNK_SAMPLES = AUDIO_SETTINGS.samples_per_chunk
    logger.info(f"CHUNK_SAMPLES = {CHUNK_SAMPLES}")
    can_send_next: asyncio.Event = asyncio.Event()

    chunks_sem = asyncio.Semaphore(settings.max_inflight_chunks)

    async def sender_generator():
        """Coroutine that yields RenderRequest messages."""
        # Send avatar image first
        avatar_path = Path(DEFAULT_IMAGE_PATH)
        if not avatar_path.exists():
            raise RuntimeError(f"Avatar image '{DEFAULT_IMAGE_PATH}' not found")
        img_bytes = avatar_path.read_bytes()
        w, h = Image.open(avatar_path).size
        yield render_service_pb2.RenderRequest(
            image=render_service_pb2.ImageChunk(data=img_bytes, width=w, height=h),
            online=True
        )
        logger.debug("Initial avatar sent to render service (%dx%d)", w, h)

        # Stream audio seconds as they appear in queue with back-pressure from video
        MAX_INFLIGHT = settings.max_inflight_chunks          # секунды аудио, которые можем отправить «вперёд»
        seconds_inflight = 0      # сколько секунд уже отправили, но ещё не «закрыли» кадрами

        speech_sended = False

        while CAN_SEND_FRAMES.is_set():
            # если отправили слишком много тишины – ждём, пока придут кадры
            # while seconds_inflight >= MAX_INFLIGHT:
            #     await can_send_next.wait()
            #     can_send_next.clear()
            #     seconds_inflight -= 1  # один долг погашен

            event = None

            if AUDIO_SECOND_QUEUE.qsize() > 0:
                logger.info(f"{AUDIO_SECOND_QUEUE.qsize()}")
                audio_sec, sr = AUDIO_SECOND_QUEUE.get_nowait()

                # Подгоняем размер до CHUNK_SAMPLES
                n = audio_sec.shape[0]
                if n < CHUNK_SAMPLES:
                    pad = np.zeros(CHUNK_SAMPLES - n, dtype=np.int16)
                    audio_sec = np.concatenate([audio_sec, pad])
                elif n > CHUNK_SAMPLES:
                    audio_sec = audio_sec[:CHUNK_SAMPLES]

                logger.info("Got buffered audio, pending %d", AUDIO_SECOND_QUEUE.qsize())
                speech_sended = True
            else:
                if speech_sended:
                    speech_sended = False
                    event = ServiceEvents.EOS if not INTERRUPT_CALLED.is_set() else ServiceEvents.INTERRUPT
                # WAV не грузится → отправляем тишину
                audio_sec, sr = np.zeros(CHUNK_SAMPLES, dtype=np.int16), AUDIO_SETTINGS.sample_rate
                logger.info("Steady silence – idle state")

            pending_audio.append((audio_sec, sr, event))

            yield render_service_pb2.RenderRequest(
                audio=render_service_pb2.AudioChunk(data=audio_sec.tobytes(), sample_rate=sr, bps=16),
                online=True,
            )
            await chunks_sem.acquire()

            #seconds_inflight += 1  # logically chunks in flight
            logger.info("Sent audio chunk to render service (inflight=%d, pending=%d)",
                        seconds_inflight, len(pending_audio))

        logger.info("Sender exited")

    frames = 0

    logger.info("Start reading chunks")

    try:
        async for chunk in stub.RenderStream(sender_generator()):
            if not CAN_SEND_FRAMES.is_set():
                logger.info("No clients - exiting receiver")
                break
            logger.info(f"New frame received, {len(frames_batch)}/{FRAMES_PER_CHUNK}")
            frames += 1
            # больше не используем событие каждые 2 кадра – управляем после минимального буфера кадров
            img = Image.frombytes("RGBA", (chunk.width, chunk.height), chunk.data, "raw", "BGRA")
            #img.save(f"images/frame_{frames}.png")
            img_np = np.asarray(
                img.convert("RGB"),
                np.uint8,
            )
            frames_batch.append(img_np)
            # Освобождаем один «токен» на каждый пришедший кадр – предотвращаем стоп при низком FPS

            # Когда набрали минимальный пакет кадров
            if len(frames_batch) == FRAMES_PER_CHUNK:
                chunks_sem.release()
                if pending_audio:
                    audio_chunk, _sr, event = pending_audio.popleft()
                    if event and event == ServiceEvents.EOS:
                        USER_EVENTS.put_nowait({"type": "eos"})
                        if ANIMATION_CALLED.is_set():
                            USER_EVENTS.put_nowait({"type": "animationEnded"})
                            ANIMATION_CALLED.clear()
                        if EMOTION_CALLED.is_set():
                            USER_EVENTS.put_nowait({"type": "emotionEnded"})
                            EMOTION_CALLED.clear()
                        await asyncio.sleep(0)
                    elif event and event == ServiceEvents.INTERRUPT:
                        USER_EVENTS.put_nowait({"type": "interrupted"})
                        await asyncio.sleep(0)
                        INTERRUPT_CALLED.clear()
                    SYNC_QUEUE.put((audio_chunk, frames_batch.copy()))
                    logger.info("SYNC_QUEUE +1 (size=%d)", SYNC_QUEUE.qsize())
                else:
                    logger.warning("Render service produced %d frames but no matching audio is pending", FRAMES_PER_CHUNK)
                can_send_next.set()  # сообщаем генератору, что можно слать ещё секунду
                frames_batch.clear()

        # ── flush remaining frames on stream end ────────────────────────
        logger.info("start flushing")
        if frames_batch:
            logger.info("Flushing remaining %d frame(s) after stream end", len(frames_batch))
            while frames_batch:
                batch = frames_batch[:FRAMES_PER_CHUNK].copy()
                del frames_batch[:FRAMES_PER_CHUNK]
                if not pending_audio:
                    logger.warning("No pending audio for remaining frames – stopping flush")
                    break
                audio_sec, _sr, _ = pending_audio.popleft()
                SYNC_QUEUE.put_nowait((audio_sec, batch))
                logger.info("Final SYNC_QUEUE +1 (flush) (size=%d)", SYNC_QUEUE.qsize())

        logger.info(f"End receiving frames, total = {frames}, pending_audio={len(pending_audio)}")

    except BaseException as e:
        logger.exception(e)
        import traceback
        logger.info(traceback.format_tb(e.__traceback__))
    finally:
        while not AUDIO_SECOND_QUEUE.empty():
            AUDIO_SECOND_QUEUE.get_nowait()
        while not SYNC_QUEUE.empty():
            SYNC_QUEUE.get_nowait()

        logger.info(f"Queues cleared")

        can_send_next.set()
        await channel.close()

def _natural_key(p: str | Path):
    """Ключ для сортировки кадров по числам: frame_1.png, frame_10.png → 1, 10."""
    s = str(p)
    l = [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]
    return l


def _read_frames_rgb(images_glob: str, fps: int = 25) -> List[List[np.ndarray]]:
    """Читает все PNG и режет на батчи по fps. Последний батч дополняется последним кадром."""
    files = sorted(glob.glob(images_glob), key=_natural_key)
    if not files:
        raise FileNotFoundError(f"Нет кадров по шаблону: {images_glob}")

    frames: List[np.ndarray] = []
    for f in files:
        img = Image.open(f).convert("RGB")
        frames.append(np.asarray(img, dtype=np.uint8))

    batches: List[List[np.ndarray]] = []
    for i in range(0, len(frames), fps):
        batch = frames[i:i + fps]
        if len(batch) < fps:
            batch += [batch[-1]] * (fps - len(batch))  # паддинг последним кадром
        batches.append(batch)
    logger.info("Прочитано %d кадров → %d батч(ей) по %d кадров",
                len(frames), len(batches), fps)
    return batches


def _resample_int16_mono(x: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Простой линейный ресемплер (без зависимостей). x: int16 mono."""
    if src_sr == dst_sr:
        return x
    n_src = x.shape[0]
    n_dst = int(round(n_src * (dst_sr / src_sr)))
    # интерполяция в float
    src_t = np.linspace(0.0, 1.0, n_src, endpoint=False, dtype=np.float64)
    dst_t = np.linspace(0.0, 1.0, n_dst, endpoint=False, dtype=np.float64)
    y = np.interp(dst_t, src_t, x.astype(np.float32))
    y = np.clip(np.rint(y), -32768, 32767).astype(np.int16)
    return y


def _read_wav_mono_16k_int16(audio_path: str, target_sr: int = 16000) -> np.ndarray:
    """Читает WAV любого кол-ва каналов, 16-бит PCM; моно и 16кГц на выходе."""
    with wave.open(audio_path, "rb") as w:
        nch = w.getnchannels()
        sampwidth = w.getsampwidth()
        sr = w.getframerate()
        nframes = w.getnframes()
        raw = w.readframes(nframes)

    if sampwidth != 2:
        raise ValueError(f"Ожидался 16-бит PCM, а пришло {8 * sampwidth}-бит. Конвертни в 16-бит.")

    data = np.frombuffer(raw, dtype="<i2")  # little-endian int16
    if nch > 1:
        # downmix → mono
        data = data.reshape(-1, nch).astype(np.int32).mean(axis=1)
        data = np.clip(data, -32768, 32767).astype(np.int16)

    if sr != target_sr:
        data = _resample_int16_mono(data, sr, target_sr)

    return data  # shape: (samples,), dtype=int16, sr=target_sr


async def stream_worker_from_disk(
    audio_path: str,
    images_glob: str = "images/frame_*.png",
    fps: int = 25,
    target_sr: int = 16000,
) -> None:
    """
    Читает WAV и PNG с диска и кладёт в SYNC_QUEUE пары (audio_sec: np.int16[16000], frames25: list[np.ndarray]).

    Правила синхронизации:
      - 1 секунда аудио (16000 сэмплов) ↔ 25 кадров.
      - Если кадров больше, чем секунд аудио — аудио дополняется нулями.
      - Если аудио длиннее — хвост игнорируется.
    """
    # --- читаем и готовим данные ---
    frames_batches = _read_frames_rgb(images_glob, fps=fps)
    audio = _read_wav_mono_16k_int16(audio_path, target_sr=target_sr)

    seconds_frames = len(frames_batches)
    samples_per_sec = target_sr
    need_samples = seconds_frames * samples_per_sec

    if audio.shape[0] < need_samples:
        pad = np.zeros(need_samples - audio.shape[0], dtype=np.int16)
        audio = np.concatenate([audio, pad], axis=0)
        logger.warning(
            "Аудио короче (%d samp) чем нужно для %d сек → дополнили нулями до %d samp",
            audio.shape[0] - pad.shape[0], seconds_frames, need_samples
        )
    elif audio.shape[0] > need_samples:
        audio = audio[:need_samples]
        logger.warning(
            "Аудио длиннее чем нужно для %d сек → обрезали до %d samp",
            seconds_frames, need_samples
        )

    # --- раскладываем по секундам и публикуем в очередь ---
    for sec_idx in range(seconds_frames):
        start = sec_idx * samples_per_sec
        end = start + samples_per_sec
        audio_sec = audio[start:end]  # np.int16, 16000 samples

        if not AUDIO_SECOND_QUEUE.qsize():
            logger.info("No chunks - send silence")
            audio_sec = np.zeros(16000, dtype=np.int16)
        else:
            logger.info("Got chunk")
            audio_sec = AUDIO_SECOND_QUEUE.get_nowait()
            audio_sec = audio_sec[0]

        frames_batch = frames_batches[sec_idx]  # list[np.ndarray], длина = fps
        SYNC_QUEUE.put((audio_sec, frames_batch))
        logger.info("SYNC_QUEUE +1 (sec=%d/%d, size=%d)",
                    sec_idx + 1, seconds_frames, SYNC_QUEUE.qsize())

    logger.info("Загрузка с диска завершена: %d секунд/батчей отправлено.", seconds_frames)

async def stream_worker_forever() -> None:
    """Run ``stream_worker_aio`` forever, restarting it after unhandled exceptions.

    Args:
        restart_delay: Seconds to wait before restarting after a crash.
    """
    while True:
        streamer = None
        try:
            await Conditions.can_process_frames()
            streamer = asyncio.create_task(stream_worker_aio())
            await streamer
            logger.warning("stream_worker_aio exited normally")
        except Exception as e:
            logger.exception(f"stream_worker_aio crashed {e!r}")
        finally:
            if streamer:
                logger.info("stream_worker_aio - task cancelled")
                try:
                    streamer.cancel()
                except:
                    pass
