"""Голосовой бот службы поддержки: AudioSocket + ARI + речь AITUNNEL.

Сетевой слой построен на asyncio; для REST/WebSocket ARI, биллинга и
STT/TTS используется aiohttp, для PCM — numpy.
"""

__version__ = "0.2.0"
