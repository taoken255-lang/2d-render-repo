"""Configuration settings for RTC Media Server."""

import os
from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings."""
    
    # Server settings
    host: str = "0.0.0.0"
    port: int = 8080
    https: bool = False
    ssl_cert: str = ""
    ssl_key: str = ""
    
    # WebSocket settings
    websocket_host: str = "0.0.0.0"
    websocket_port: int = 8001
    
    # gRPC settings
    grpc_server_url: str = "localhost:8502" #"2d.digitalavatars.ru"  # Default 2d-render port
    grpc_secure_channel: bool = False

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

    debug_page_enabled: bool = True


# Global settings instance
settings = Settings() 