"""Тесты реестра ожидающих вызовов."""

from __future__ import annotations

import asyncio

import pytest

from app.audiosocket.registry import PendingCall, PendingRegistry


class FakeSession:
    def __init__(self) -> None:
        self.digits: list[str] = []

    def push_digit(self, digit: str) -> None:
        self.digits.append(digit)


def test_digits_are_buffered_before_connection() -> None:
    call = PendingCall(uuid="u-1", channel_id="call-1", caller_number="7900")
    call.push_digit("1")
    call.push_digit("2")
    assert call.drain_digits() == ["1", "2"]
    assert call.drain_digits() == []


def test_digits_go_to_session_after_attach() -> None:
    call = PendingCall(uuid="u-1", channel_id="call-1")
    session = FakeSession()
    call.attach(session)
    assert call.connected.is_set()

    call.push_digit("5")
    assert session.digits == ["5"]
    assert call.drain_digits() == []


def test_close_calls_callback_once() -> None:
    reasons: list[str] = []

    async def on_close(reason: str) -> None:
        reasons.append(reason)

    call = PendingCall(uuid="u-1", channel_id="call-1", on_close=on_close)

    async def scenario() -> None:
        await call.close("first")
        await call.close("second")

    asyncio.run(scenario())
    assert reasons == ["first"]
    assert call.is_closed
    assert call.connected.is_set()


def test_registry_operations() -> None:
    registry = PendingRegistry()
    call = PendingCall(uuid="u-1", channel_id="call-1")
    registry.register(call)

    assert len(registry) == 1
    assert "u-1" in registry
    assert registry.get("u-1") is call
    with pytest.raises(ValueError):
        registry.register(call)

    assert registry.pop("u-1") is call
    assert len(registry) == 0
    assert registry.discard("u-1") is None
    assert registry.values() == ()
