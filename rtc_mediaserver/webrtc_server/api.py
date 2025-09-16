"""FastAPI application exposing WebRTC endpoints and single control websocket."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCDataChannel, RTCConfiguration  # type: ignore
from aiortc.rtcrtpsender import RTCRtpSender  # type: ignore
from starlette.middleware.cors import CORSMiddleware

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
        return
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
            status_code=500,
            content={"error": "Failed to get info data"}
        )


# ───────────────────────── WebRTC offer logic ──────────────────────────


async def process_offer(params: Dict[str, Any]) -> Dict[str, Any]:
    """Create peer connection and return answer dict for /offer route."""

    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    session = rand_id()
    pc = RTCPeerConnection()

    await pc.setRemoteDescription(offer)

    player = WebRTCMediaPlayer()
    pc.addTrack(player.audio)
    pc.addTrack(player.video)

    # Prefer VP8 / H264 codecs for video
    for t in pc.getTransceivers():
        if t.kind == "video":
            caps = RTCRtpSender.getCapabilities("video")
            prefs = [c for c in caps.codecs if c.name in ("VP8", "H264")]
            t.setCodecPreferences(prefs)

    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
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
                AVATAR_SET.clear()
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
    if RTC_STREAM_CONNECTED.locked():
        return JSONResponse(status_code=423, content=
            {
              "type": "error",
              "code": "SERVICE_BUSY",
              "message": "Reached max count of connected clients. Service busy."
            }
        )
    params = await request.json()
    answer_dict = await process_offer(params)
    return JSONResponse(answer_dict)

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
