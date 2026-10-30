"""Сетевые помощники: локальные адреса и внешний IP.

Нужны, чтобы подсказать оператору, какой адрес указывать в ExternalMedia
(Asterisk подключается к нам сам, поэтому 127.0.0.1 тут не подходит).
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request

PUBLIC_IP_URL = "https://api.ipify.org"


def local_ipv4_addresses() -> list[str]:
    """Все локальные IPv4-адреса машины (без loopback и без дублей)."""
    addresses: set[str] = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            addresses.add(str(info[4][0]))
    except OSError:
        pass

    # Адрес, через который пойдёт исходящий трафик (UDP-connect пакетов не шлёт).
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            addresses.add(str(probe.getsockname()[0]))
    except OSError:
        pass

    return sorted(addr for addr in addresses if not addr.startswith("127."))


def primary_ipv4() -> str | None:
    """Наиболее вероятный адрес для входящих подключений."""
    addresses = local_ipv4_addresses()
    return addresses[0] if addresses else None


def public_ipv4(timeout: float = 5.0) -> str | None:
    """Внешний IP (для диагностики; может быть недоступен без интернета)."""
    try:
        with urllib.request.urlopen(PUBLIC_IP_URL, timeout=timeout) as response:
            return response.read(64).decode("ascii", "ignore").strip() or None
    except (urllib.error.URLError, OSError, ValueError):
        return None
