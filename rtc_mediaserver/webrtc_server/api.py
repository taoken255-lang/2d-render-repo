"""FastAPI application exposing WebRTC endpoints and single control websocket."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Dict, Optional, Union

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCDataChannel, RTCConfiguration  # type: ignore
from aiortc.rtcrtpsender import RTCRtpSender  # type: ignore
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response

from rtc_mediaserver.logging_config import get_logger, setup_default_logging
from .constants import CAN_SEND_FRAMES, RTC_STREAM_CONNECTED, WS_CONTROL_CONNECTED, USER_EVENTS, AVATAR_SET, INIT_DONE, \
    STATE, State
from .grpc_client import stream_worker_forever
from .player import WebRTCMediaPlayer
from .handlers import HANDLERS, ClientState
from .info import info
from ..config import settings

# Ensure logging configured
setup_default_logging()
logger = get_logger(__name__)

app = FastAPI(title="Threaded WebRTC Server")

# Exception handler for invalid JSON
@app.exception_handler(JSONDecodeError)
async def json_decode_error_handler(request: Request, exc: JSONDecodeError):
    return JSONResponse(
        status_code=400,
        content={
            "type": "error", 
            "code": "UNKNOWN_ERROR",
            "message": "Unknown error occured."
        }
    )

origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Path to HTML client template
TEMPLATES_DIR = Path(__file__).parent / "templates"
HTML_FILE = TEMPLATES_DIR / "index.html"


def rand_id() -> int:
    return random.randint(100_000, 999_999)


@app.on_event("startup")
async def _startup_event() -> None:
    """Launch gRPC stream worker with auto-restart on app startup."""
    t = asyncio.create_task(stream_worker_forever())
    logger.info("gRPC aio worker task created (auto-restart enabled)")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:  # type: ignore[override]
    if not settings.debug_page_enabled:
        return Response(status_code=404)
    return HTMLResponse(HTML_FILE.read_text(encoding="utf-8"))


@app.get("/info")
async def get_info() -> JSONResponse:
    """Get available avatars with their animations and emotions."""
    try:
        info_data = info()
        return JSONResponse(info_data)
    except Exception as e:
        logger.error(f"Error getting info data: {e}")
        return JSONResponse(
            status_code=400,
            content={
                "type": "error",
                "code": "UNKNOWN_ERROR",
                "message": "Unknown error occured."
            }
        )

# ───────────────────────── WebRTC offer logic ──────────────────────────
def sdp_set_bandwidth(sdp: str, *, video_kbps: int = 4000, audio_kbps: int = 128, framerate: int = 25) -> str:
    """Простой SDP-мунджер: задаёт b=AS для audio/video и a=framerate для видео."""
    lines = sdp.splitlines()
    out = []
    in_video = False
    in_audio = False

    def inject_video_params(dst: list):
        # Сносим уже существующие ограничения, чтобы не дублировать
        while dst and (dst[-1].startswith("b=AS:") or dst[-1].startswith("b=TIAS:") or dst[-1].startswith("a=framerate:")):
            dst.pop()
        dst.append(f"b=AS:{video_kbps}")
        dst.append(f"a=framerate:{framerate}")

    def inject_audio_params(dst: list):
        while dst and (dst[-1].startswith("b=AS:") or dst[-1].startswith("b=TIAS:")):
            dst.pop()
        dst.append(f"b=AS:{audio_kbps}")

    for i, ln in enumerate(lines):
        # Начало секций
        if ln.startswith("m=video"):
            in_video, in_audio = True, False
            out.append(ln)
            continue
        if ln.startswith("m=audio"):
            in_video, in_audio = False, True
            out.append(ln)
            continue
        if ln.startswith("m="):  # любая другая секция
            # перед уходом из предыдущей секции дольём параметры (если не успели)
            if in_video:
                inject_video_params(out)
            if in_audio:
                inject_audio_params(out)
            in_video = in_audio = False
            out.append(ln)
            continue

        # Копим строки секции
        out.append(ln)

        # Евристика: когда встречается следующая "a=" или "c=" — мы всё равно добавим в конце секции,
        # поэтому основной инжект сделаем при переключении секций и после прохода.
        # Ничего не делаем тут.

    # Финальный инжект, если файл закончился внутри audio/video
    if in_video:
        inject_video_params(out)
    if in_audio:
        inject_audio_params(out)

    return "\r\n".join(out) + "\r\n"

import asyncio, logging, time
from typing import Dict, Any, Tuple

log = logging.getLogger("webrtc.encoder")

def _g(o: Any, name: str, default=None):
    return getattr(o, name, default)

async def sample_encoder(pc, interval: float = 2.0):
    """
    Снимает энкодерные метрики для ВИДЕО из outbound-rtp:
    - avg_enc_ms: Δ(totalEncodeTime)/Δ(framesEncoded)*1000
    - eff_fps:    Δ(framesEncoded)/Δt (если нет track.fps)
    - kbps:       Δ(bytesSent)*8/Δt/1000
    """
    prev: Dict[str, Dict[str, float]] = {}   # по ключу (ssrc|mid|id)
    prev_ts: float | None = None

    while True:
        started = time.perf_counter()
        try:
            report = await pc.getStats()

            outbound: Dict[str, Any] = {}
            # Собираем ВСЕ outbound-rtp, а потом фильтруем по mediaType/kind
            for s in report.values():
                if _g(s, "type") == "outbound-rtp" and not _g(s, "isRemote", False):
                    key = str(_g(s, "ssrc") or _g(s, "mid") or _g(s, "id"))
                    outbound[key] = s

            now = time.perf_counter()
            dt = max(1e-9, (now - (prev_ts or now)))
            lines = []

            for key, o in outbound.items():
                media = (_g(o, "mediaType") or _g(o, "kind") or "").lower()
                if media != "video":  # <-- ✅ главный фикс: определяем видео без trackId
                    continue

                frames_total = float(_g(o, "framesEncoded", 0.0) or 0.0)
                enc_time_total = float(_g(o, "totalEncodeTime", 0.0) or 0.0)  # секунды
                bytes_total = float(_g(o, "bytesSent", 0.0) or 0.0)

                p = prev.get(key, {"frames": frames_total, "enc_time": enc_time_total, "bytes": bytes_total})
                d_frames = max(0.0, frames_total - p["frames"])
                d_enc_time = max(0.0, enc_time_total - p["enc_time"])  # сек
                d_bytes = max(0.0, bytes_total - p["bytes"])

                avg_enc_ms = (d_enc_time / d_frames * 1000.0) if d_frames > 0 else None
                eff_fps = (d_frames / dt) if d_frames > 0 else None
                kbps = (d_bytes * 8.0 / dt) / 1000.0

                lines.append({
                    "stream": key,
                    "eff_fps": round(eff_fps, 2) if eff_fps is not None else None,
                    "frames+": int(d_frames),
                    "avg_enc_ms": round(avg_enc_ms, 3) if avg_enc_ms is not None else None,
                    "kbps": int(kbps),
                })

                prev[key] = {"frames": frames_total, "enc_time": enc_time_total, "bytes": bytes_total}

            prev_ts = now

            if lines:
                s = " | ".join(
                    f"video[{x['stream']}] eff_fps={x['eff_fps']} frames+={x['frames+']} "
                    f"avg_enc_ms={x['avg_enc_ms']} kbps={x['kbps']}"
                    for x in lines
                )
                log.info("[ENCODER] %s", s)
            else:
                # Помогаем себе диагностикой — какие outbound вообще видим
                kinds_seen = [(_g(o, "mediaType") or _g(o, "kind")) for o in outbound.values()]
                log.info("[ENCODER] no VIDEO outbound tracks yet (seen outbound=%s)", kinds_seen or "[]")

        except Exception:
            log.exception("encoder sampler error")

        # стабильный период
        next_tick = started + interval
        await asyncio.sleep(max(0.0, next_tick - time.perf_counter()))

async def process_offer(params: Dict[str, Any]) -> Dict[str, Any]:
    """Create peer connection and return answer dict for /offer route."""

    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    session = rand_id()
    pc = RTCPeerConnection()

    # FIX качество: создаём треки и задаём предпочтения кодеков ДО setRemoteDescription
    player = WebRTCMediaPlayer()
    pc.addTrack(player.audio)
    pc.addTrack(player.video)

    # FIX качество: Жёстко предпочитаем H264 с packetization-mode=1 (по опыту стабильнее для FullHD)
    for t in pc.getTransceivers():
        if t.kind == "video":
            caps = RTCRtpSender.getCapabilities("video")
            h264_pmode1 = [
                c for c in caps.codecs
                if c.name == "H264" and c.parameters.get("packetization-mode") == "1"
            ]
            if h264_pmode1:
                t.setCodecPreferences(h264_pmode1)
            # если хочешь оставить fallback на VP8 — можно добавить сюда ветку

    # Теперь применяем удалённый оффер
    await pc.setRemoteDescription(offer)

    # FIX качество: создаём answer, мундjim SDP — битрейт и FPS
    answer = await pc.createAnswer()
    munged_sdp = sdp_set_bandwidth(
        answer.sdp,
        video_kbps=4000,  # FIX качество: подними/понизь по нуждам (пример: 6000 для FullHD 25 fps)
        audio_kbps=128,
        framerate=25,
    )
    await pc.setLocalDescription(RTCSessionDescription(sdp=munged_sdp, type=answer.type))

    logger.info("session %s established", session)
    async def remove_client_by_timeout():
        logger.info(f">> killer wait for timeout to close webrtc channel for client {session}")
        await asyncio.sleep(float(settings.uninitialized_rtc_kill_timeout))
        logger.info(f"<< killer closing webrtc channel for client {session}")
        await pc.close()

    killer_task = asyncio.create_task(remove_client_by_timeout())

    @pc.on("connectionstatechange")
    async def on_connection_state_change():  # noqa: D401
        if pc.connectionState == "connected":

            if not killer_task.cancelled() or not killer_task.done():
                killer_task.cancel()
            try:
                await asyncio.wait_for(RTC_STREAM_CONNECTED.acquire(), 0.1)
                logger.info(f"Peer connected {session}")
                logger.info("CAN_SEND_FRAMES.set()")
                CAN_SEND_FRAMES.set()
                State.current_session_id = session
                STATE.current_pc = pc
            except asyncio.TimeoutError:
                logger.info(f"Peer tried to connect to locked resource {session}")
                await pc.close()

        elif pc.connectionState in ("failed", "disconnected", "closed"):
            if not killer_task.cancelled() or not killer_task.done():
                killer_task.cancel()
            logger.info(f"Peer disconnected {session} (state={pc.connectionState}) – cleaning up")
            if session == State.current_session_id:
                logger.info("CAN_SEND_FRAMES.clear()")
                CAN_SEND_FRAMES.clear()
                STATE.auto_idle = True
                try:
                    RTC_STREAM_CONNECTED.release()
                except ValueError as e:
                    logger.error(f"RTC_STREAM_CONNECTED.release() -> {e!r}")
                STATE.kill_streamer()
            await pc.close()

    return {
        "sdp": pc.localDescription.sdp,
        "type": pc.localDescription.type,
    }


@app.post("/offer")
async def offer(request: Request):  # type: ignore[override]
    # Check Content-Type header
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith("application/json"):
        return JSONResponse(
            status_code=400,
            content={
                "type": "error",
                "code": "UNKNOWN_ERROR",
                "message": "Unknown error occured."
            }
        )
    
    if RTC_STREAM_CONNECTED.locked():
        await STATE.current_pc.close()
        # return JSONResponse(status_code=423, content=
        #     {
        #       "type": "error",
        #       "code": "SERVICE_BUSY",
        #       "message": "Reached max count of connected clients. Service busy."
        #     }
        # )
    
    try:
        params = await request.json()
    except JSONDecodeError:
        return JSONResponse(
            status_code=400,
            content={
                "type": "error",
                "code": "UNKNOWN_ERROR",
                "message": "Unknown error occured."
            }
        )

    request_type = params.get("type", None)
    sdp = params.get("sdp", None)

    if not params or not request_type or not sdp or request_type != "offer":
        return JSONResponse(
            status_code=400,
            content={
                "type": "error",
                "code": "UNKNOWN_ERROR",
                "message": "Unknown error occured."
            }
        )

    try:
        answer_dict = await process_offer(params)
        return JSONResponse(answer_dict)
    except:
        return JSONResponse(
            status_code=400,
            content={
                "type": "error",
                "code": "UNKNOWN_ERROR",
                "message": "Unknown error occured."
            }
        )

# ───────────────────────── Control websocket ───────────────────────────

async def send_user_event(websocket: WebSocket):
    logger.info("Started user events watchdog")
    while True:
        message = await USER_EVENTS.get()
        logger.info(f"Send event {message}")
        await websocket.send_json(message)

@app.websocket("/ws")
async def control_ws(websocket: WebSocket):  # type: ignore[override]
    """Single websocket channel handling control/audio messages."""
    await websocket.accept()

    if WS_CONTROL_CONNECTED.locked():
        await websocket.send_json({
              "type": "error",
              "code": "SERVICE_BUSY",
              "message": "Reached max count of connected clients. Service busy."
            })
        await websocket.close()
        return

    try:
        await asyncio.wait_for(WS_CONTROL_CONNECTED.acquire(), 0.1)
    except asyncio.TimeoutError:
        await websocket.send_json({
            "type": "error",
            "code": "SERVICE_BUSY",
            "message": "Reached max count of connected clients. Service busy."
        })
        await websocket.close()
        return

    state = ClientState()

    eos_watcher = asyncio.create_task(send_user_event(websocket))

    try:
        while True:
            data_text = await websocket.receive_text()
            try:
                message = json.loads(data_text)
                msg_type = message.get("type")
                if msg_type is None:
                    raise ValueError("Message missing 'type' field")
                handler = HANDLERS.get(msg_type)
                if handler is None:
                    raise ValueError(f"Unknown message type '{msg_type}'")
                logger.info("Received WS command: %s", msg_type)
                result = await handler(message, state)
                if isinstance(result, dict):
                    await websocket.send_json(result)
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"Error processing WS message, {data_text}, {exc!r}")
                await websocket.send_json({
                  "type": "error",
                  "code": "UNKNOWN_ERROR",
                  "message": "Unknown error occured."
                })
    except WebSocketDisconnect:
        logger.info("Control websocket disconnected")
    finally:
        eos_watcher.cancel()
        try:
            WS_CONTROL_CONNECTED.release()
        except ValueError as e:
            logger.error(f"WS_CONTROL_CONNECTED.release() -> {e!r}")
        INIT_DONE.clear()


@app.on_event("shutdown")
async def _shutdown() -> None:
    logger.info("Application shutdown – all peer connections closed")
