"""Тесты LLM-слоя: chat/completions, function calling и промпты."""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from aiohttp import web

from app.billing.client import BillingError, Subscriber
from app.config import Settings
from app.llm.client import LLMClient
from app.llm.conversation import ChatConversation, clean_for_speech
from app.llm.errors import LLMError
from app.llm.prompts import LLMPromptError, LLMPromptSet, load_llm_prompts
from app.llm.tools import CallControl, VoiceTools

TOKEN = "sk-aitunnel-llm-test"
SYSTEM = "Ты голосовой ассистент поддержки."


class FakeLLM:
    """Мини-сервер AITUNNEL /chat/completions с очередью ответов."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.status = 200
        self.requests: list[dict[str, Any]] = []
        self.headers: dict[str, str] = {}
        self._socket: socket.socket | None = None
        self._runner: web.AppRunner | None = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self._handle)
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(64)
        self.port = int(self._socket.getsockname()[1])
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.SockSite(self._runner, self._socket).start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        self.headers = dict(request.headers)
        self.requests.append(await request.json())
        if self.status != 200:
            return web.Response(status=self.status, text="llm failed")
        message = self.messages.pop(0) if self.messages else {"role": "assistant", "content": ""}
        return web.json_response({"choices": [{"message": message}]})


class FakeBilling:
    def __init__(
        self,
        subscriber: Subscriber | None = None,
        error: BillingError | None = None,
    ) -> None:
        self.subscriber = subscriber
        self.error = error
        self.user_ids: list[str] = []

    async def fetch_subscriber(self, user_id: str) -> Subscriber | None:
        self.user_ids.append(user_id)
        if self.error is not None:
            raise self.error
        return self.subscriber


def _subscriber() -> Subscriber:
    return Subscriber(
        user_id="1",
        balance=227.23,
        packet_name="Интернет 100 Мбит",
        packet_sum=200.0,
        days_left=12,
        network_state="green",
        network_reason="связь есть",
        raw={
            "id": "1",
            "extra_account": 227.23,
            "packet_name": "Интернет 100 Мбит",
            "packet_sum": 200,
            "days_left": 12,
            "packet_id": "242",
            "auto_activation_enabled": True,
            "network_status": {
                "switch_state": "green",
                "is_in_network_failure_zone": False,
                "reason": "связь есть",
                "snmp_flag": "secret",
            },
            "active_repair": False,
        },
    )


def _settings(fake: FakeLLM) -> Settings:
    return Settings(
        audiosocket_host="127.0.0.1",
        audiosocket_port=0,
        scenario_path=Path("scenario.json"),
        prompts_dir=Path("prompts"),
        record_calls=False,
        recordings_dir=Path("recordings"),
        init_timeout=1.0,
        idle_timeout=1.0,
        max_call_seconds=1.0,
        log_level="WARNING",
        ai_tunnel_api_key=TOKEN,
        ai_tunnel_base_url=fake.url,
        llm_model="gpt-4o-mini",
        llm_temperature=0.2,
        llm_max_tokens=123,
    )


def _prompts() -> LLMPromptSet:
    return LLMPromptSet(system=SYSTEM, base=SYSTEM, knowledge=MappingProxyType({}))


def _tool_call(
    name: str,
    arguments: str,
    call_id: str = "call_1",
    content: str | None = None,
) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ],
    }


def test_chat_completion_sends_tools_and_model_options() -> None:
    async def scenario() -> None:
        fake = FakeLLM()
        fake.messages = [{"role": "assistant", "content": "Здравствуйте!"}]
        await fake.start()
        try:
            async with LLMClient(_settings(fake), _prompts()) as client:
                tools = VoiceTools(FakeBilling(_subscriber()))
                conversation = client.conversation(tools)
                reply = await conversation.ask("Здравствуйте")
        finally:
            await fake.stop()

        assert reply.text == "Здравствуйте!"
        request = fake.requests[0]
        assert request["model"] == "gpt-4o-mini"
        assert request["temperature"] == pytest.approx(0.2)
        assert request["max_tokens"] == 123
        assert request["tool_choice"] == "auto"
        assert {tool["function"]["name"] for tool in request["tools"]} == {
            "get_billing_info",
            "transfer_to_operator",
            "end_call",
        }
        assert fake.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request["messages"][0] == {"role": "system", "content": SYSTEM}
        assert request["messages"][1] == {"role": "user", "content": "Здравствуйте"}

    asyncio.run(scenario())


def test_tool_call_uses_billing_and_hides_technical_fields() -> None:
    async def scenario() -> None:
        fake = FakeLLM()
        billing = FakeBilling(_subscriber())
        fake.messages = [
            _tool_call("get_billing_info", '{"user_id": "1"}'),
            {
                "role": "assistant",
                "content": "**На вашем счёте** 227 рублей 23 копейки.",
            },
        ]
        await fake.start()
        try:
            async with LLMClient(_settings(fake), _prompts()) as client:
                conversation = client.conversation(VoiceTools(billing))
                reply = await conversation.ask("Какой у меня баланс, счёт один?")
        finally:
            await fake.stop()

        assert billing.user_ids == ["1"]
        assert reply.text == "На вашем счёте 227 рублей 23 копейки."
        assert reply.end_call is False
        assert reply.transfer is False
        assert reply.tool_calls == 1
        assert conversation.tool_calls == 1

        second = fake.requests[1]["messages"]
        tool_messages = [message for message in second if message["role"] == "tool"]
        assert len(tool_messages) == 1
        assert tool_messages[0]["tool_call_id"] == "call_1"
        payload = json.loads(tool_messages[0]["content"])
        assert payload["found"] is True
        assert payload["user"]["extra_account"] == pytest.approx(227.23)
        assert payload["user"]["network_status"]["switch_state"] == "green"
        assert "packet_id" not in tool_messages[0]["content"]
        assert "snmp_flag" not in tool_messages[0]["content"]

    asyncio.run(scenario())


def test_end_call_and_transfer_tools_control_the_call() -> None:
    async def scenario() -> None:
        fake = FakeLLM()
        fake.messages = [
            _tool_call("end_call", "{}", content="Всего доброго!"),
        ]
        await fake.start()
        try:
            async with LLMClient(_settings(fake), _prompts()) as client:
                conversation = client.conversation(VoiceTools(None))
                reply = await conversation.ask("До свидания")
        finally:
            await fake.stop()

        assert reply.text == "Всего доброго!"
        assert reply.end_call is True
        assert reply.transfer is False

        control = CallControl()
        tools = VoiceTools(None)
        await tools.execute("transfer_to_operator", "{}", control)
        assert control.transfer is True
        assert control.end_call is False

    asyncio.run(scenario())


def test_billing_tool_validation_and_errors() -> None:
    async def scenario() -> None:
        tools = VoiceTools(None)
        control = CallControl()
        invalid = await tools.execute("get_billing_info", {"user_id": "1-abc"}, control)
        assert json.loads(invalid)["error"] == "invalid_user_id"
        missing = await tools.execute("get_billing_info", {"user_id": "1"}, control)
        assert json.loads(missing)["error"] == "billing_not_configured"

        broken = VoiceTools(FakeBilling(error=BillingError("нет сети")))
        failed = await broken.execute("get_billing_info", {"user_id": "1"}, control)
        assert json.loads(failed)["error"] == "billing_unavailable"
        not_found = await VoiceTools(FakeBilling(None)).execute(
            "get_billing_info", {"user_id": "999"}, control
        )
        assert json.loads(not_found) == {"found": False, "user": None}

    asyncio.run(scenario())


def test_llm_http_error_is_wrapped() -> None:
    async def scenario() -> None:
        fake = FakeLLM()
        fake.status = 500
        await fake.start()
        try:
            async with LLMClient(_settings(fake), _prompts()) as client:
                conversation = client.conversation(VoiceTools(None))
                with pytest.raises(LLMError, match="HTTP 500"):
                    await conversation.ask("привет")
        finally:
            await fake.stop()

    asyncio.run(scenario())


def test_clean_for_speech_removes_markdown() -> None:
    text = "## Тариф\n- **100 Мбит** — 200 ₽\n[Подробнее](https://example.com)\n```json\n{}\n```"
    assert clean_for_speech(text) == "Тариф 100 Мбит — 200 ₽ Подробнее"


def test_llm_prompts_are_composed(tmp_path: Path) -> None:
    (tmp_path / "support_evp.md").write_text("Основной промпт", encoding="utf-8")
    (tmp_path / "billing_account_data.md").write_text("Правила биллинга", encoding="utf-8")
    (tmp_path / "connection_terms.md").write_text("Условия подключения", encoding="utf-8")
    (tmp_path / "internet_private.md").write_text("Тарифы", encoding="utf-8")

    prompts = load_llm_prompts(tmp_path)
    assert prompts.document_count == 4
    assert "Основной промпт" in prompts.system
    assert "Правила биллинга" in prompts.system
    assert "Условия подключения" in prompts.system
    assert "Тарифы" in prompts.system
    assert "get_billing_info" in prompts.system
    assert prompts.knowledge_names == (
        "billing_account_data.md",
        "connection_terms.md",
        "internet_private.md",
    )


def test_llm_prompts_require_base(tmp_path: Path) -> None:
    with pytest.raises(LLMPromptError, match="support_evp.md"):
        load_llm_prompts(tmp_path)

