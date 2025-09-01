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
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCDataChannel  # type: ignore
from aiortc.rtcrtpsender import RTCRtpSender  # type: ignore

from rtc_mediaserver.logging_config import get_logger, setup_default_logging
from .constants import CAN_SEND_FRAMES, RTC_STREAM_CONNECTED, WS_CONTROL_CONNECTED, USER_EVENTS, AVATAR_SET, INIT_DONE
from .grpc_client import stream_worker_forever
from .player import WebRTCMediaPlayer
from .handlers import HANDLERS, ClientState
from ..config import settings

# Ensure logging configured
setup_default_logging()
logger = get_logger(__name__)

app = FastAPI(title="Threaded WebRTC Server")

pcs: Dict[int, RTCPeerConnection] = {}

# Path to HTML client template
TEMPLATES_DIR = Path(__file__).parent / "templates"
HTML_FILE = TEMPLATES_DIR / "index.html"


def rand_id() -> int:
    return random.randint(100_000, 999_999)


@app.on_event("startup")
async def _startup_event() -> None:
    """Launch gRPC stream worker with auto-restart on app startup."""
    asyncio.create_task(stream_worker_forever())
    logger.info("gRPC aio worker task created (auto-restart enabled)")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:  # type: ignore[override]
    return HTMLResponse(HTML_FILE.read_text(encoding="utf-8"))


# ───────────────────────── WebRTC offer logic ──────────────────────────


async def process_offer(params: Dict[str, Any]) -> Dict[str, Any]:
    """Create peer connection and return answer dict for /offer route."""
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    session = rand_id()
    pc = RTCPeerConnection()
    pcs[session] = pc

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
        await asyncio.sleep(settings.uninitialized_rtc_kill_timeout)
        logger.info(f"killer closing webrtc channel for client {session}")
        await pc.close()
        pcs.pop(session, None)

    killer_task = asyncio.create_task(remove_client_by_timeout())

    @pc.on("connectionstatechange")
    async def on_connection_state_change():  # noqa: D401
        if pc.connectionState == "connected":
            killer_task.cancel()
            RTC_STREAM_CONNECTED.set()
            logger.info("Peer connected")
            logger.info("CAN_SEND_FRAMES.set()")
            CAN_SEND_FRAMES.set()
        elif pc.connectionState in ("failed", "disconnected", "closed"):
            logger.info("Peer disconnected (state=%s) – cleaning up", pc.connectionState)
            logger.info("CAN_SEND_FRAMES.clear()")
            CAN_SEND_FRAMES.clear()
            await pc.close()
            pcs.pop(session, None)
            RTC_STREAM_CONNECTED.clear()

    return {
        "sdp": pc.localDescription.sdp,
        "type": pc.localDescription.type,
    }


@app.post("/offer")
async def offer(request: Request):  # type: ignore[override]
    if RTC_STREAM_CONNECTED.is_set():
        return JSONResponse(status_code=423, content=
            {
              "type": "error",
              "code": "SERVICE_BUSY",
              "message": "Reached max count of connected clients. Service busy."
            }
        )
    # RTC_STREAM_CONNECTED.set()
    params = await request.json()
    answer_dict = await process_offer(params)
    return JSONResponse(answer_dict)


@app.post("/destroy")
async def destroy(request: Request):  # type: ignore[override]
    data = await request.json()
    sess = data.get("session")
    pc: Optional[RTCPeerConnection] = pcs.pop(sess, None)
    if pc:
        await pc.close()
    return JSONResponse({"status": "done"})


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

    if WS_CONTROL_CONNECTED.is_set():
        await websocket.send_json({
              "type": "error",
              "code": "SERVICE_BUSY",
              "message": "Reached max count of connected clients. Service busy."
            })
        await websocket.close()
        return

    WS_CONTROL_CONNECTED.set()
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
        WS_CONTROL_CONNECTED.clear()
        logger.info("Control websocket disconnected")
    finally:
        eos_watcher.cancel()
        AVATAR_SET.clear()
        INIT_DONE.clear()


@app.on_event("shutdown")
async def _shutdown() -> None:
    for pc in list(pcs.values()):
        await pc.close()
    logger.info("Application shutdown – all peer connections closed")
