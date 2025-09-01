"""Configuration settings for RTC Media Server."""

import os
from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings."""
    
    # Server settings
    host: str = "0.0.0.0"
    port: int = 8080
    
    # WebSocket settings
    websocket_host: str = "0.0.0.0"
    websocket_port: int = 8001
    
    # gRPC settings
    grpc_server_url: str = "localhost:8500" #"2d.digitalavatars.ru"  # Default 2d-render port
    grpc_secure_channel: bool = False
    
    # WebRTC settings
    # ICE servers help establish P2P connections through NAT/firewalls
    # STUN servers help discover public IP addresses
    # TURN servers relay data when direct connection is impossible
    webrtc_ice_servers: list = [
        {"urls": ["stun:stun.l.google.com:19302"]},
        # Add TURN servers for production:
        # {
        #     "urls": ["turn:your-turn-server.com:3478"],
        #     "username": "username",
        #     "credential": "password"
        # }
    ]
    
    # Audio settings
    audio_sample_rate: int = 16000
    audio_channels: int = 1
    audio_format: str = "s16le"
    audio_chunk_duration: float = 1.0  # Duration of audio chunks sent to 2d-render (seconds)
    
    # Video settings
    video_width: int = 640
    video_height: int = 480
    video_fps: int = 30
    
    # Logging settings
    log_level: str = "DEBUG"
    log_format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    uninitialized_rtc_kill_timeout: int = 60
    max_inflight_chunks: int = 2


# Global settings instance
settings = Settings() 