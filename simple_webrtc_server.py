#!/usr/bin/env python3
"""Compatibility wrapper around the refactored WebRTC server package.

The actual implementation now lives under ``rtc_mediaserver.webrtc_server``.
This file is kept to avoid breaking existing ``uvicorn`` invocation paths such as

    uvicorn simple_webrtc_server:app --host 0.0.0.0 --port 8080

Feel free to import ``rtc_mediaserver.webrtc_server`` directly in new code.
"""
from rtc_mediaserver.config import settings
from rtc_mediaserver.logging_config import setup_default_logging, get_logger

# Configure logging using unified formatter
setup_default_logging()
logger = get_logger(__name__)

# Re-export FastAPI application instance
from rtc_mediaserver.webrtc_server import app  # noqa: E402  (import after logging setup)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    logger.info(f"Starting Simple WebRTC Server → http://localhost:{settings.port}")
    uvicorn.run(
        "rtc_mediaserver.webrtc_server.api:app",
        host="0.0.0.0",
        port=settings.port,
        reload=True,
        access_log=True
    )
