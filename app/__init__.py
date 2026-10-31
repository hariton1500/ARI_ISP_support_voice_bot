"""Голосовой бот службы поддержки: AudioSocket + ARI + речь и LLM AITUNNEL.

Сетевой слой построен на asyncio; для REST/WebSocket ARI, биллинга,
STT/TTS и chat/completions используется aiohttp, для PCM — numpy.
"""

__version__ = "0.3.0"
