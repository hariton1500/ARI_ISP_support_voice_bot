"""Тесты реестра ожидающих вызовов."""

from __future__ import annotations

import asyncio

import pytest

from app.audiosocket.registry import PendingCall, PendingRegistry


def test_attach_marks_call_connected() -> None:
    call = PendingCall(uuid="u-1", channel_id="call-1")
    assert not call.connected.is_set()
    call.attach()
    assert call.connected.is_set()


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
