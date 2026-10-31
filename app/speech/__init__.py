"""Речь: AITUNNEL STT/TTS, VAD, эндпоинтинг и barge-in."""

from app.speech.client import SpeechBackend, SpeechClient, SpeechError
from app.speech.streaming import SpeechEvent, StreamingVoiceInput
from app.speech.vad import VadConfig, VadEvent, VadEventKind, VoiceActivityDetector

__all__ = [
    "SpeechBackend",
    "SpeechClient",
    "SpeechError",
    "SpeechEvent",
    "StreamingVoiceInput",
    "VadConfig",
    "VadEvent",
    "VadEventKind",
    "VoiceActivityDetector",
]
