#!/bin/bash

echo "Starting Render"

python3 /app/server.py &

echo "Starting WebRTC service"
ls -la /rtc_mediaserver
uv run simple_webrtc_server.py --host 0.0.0.0 --port 8080