"""Offline wire tests: the official SDKs talk only to an in-memory transport."""

from __future__ import annotations

import asyncio
import json
from dataclasses import FrozenInstanceError

import httpx
import pytest

from src.preparation.providers import (
    ProviderError,
    ProviderIdentity,
    ProviderRequestTooLarge,
    ProviderTurn,
    ToolCall,
    ToolDefinition,
    ToolFeedback,
    Usage,
    open_provider,
)


_ENDPOINTS = {
    "gemini": ("https://gemini.practice.test", "/v1beta/models/practice-model:generateContent"),
    "openai": ("https://openai.practice.test/v1", "/v1/responses"),
    "anthropic": ("https://anthropic.practice.test", "/v1/messages"),
}
_MODEL = "practice-model"
_KEY = "explicit-practice-key"
_LIMITS = {"max_output_tokens": 128, "max_request_bytes": 32768}
_ARGUMENTS = {"observationId": "obs-1", "controlRef": "sec-1"}
_PARAMETERS = {
    "type": "object",
    "properties": {"observationId": {"type": "string"}, "controlRef": {"type": "string"}},
    "required": ["observationId", "controlRef"],
    "additionalProperties": False,
}
_TOOLS = (ToolDefinition("open_section", "Open an approved section", _PARAMETERS),)
_FACTS_SCHEMA = {
    "type": "object",
    "properties": {"facts": {"type": "array", "items": {"type": "string"}}},
    "required": ["facts"],
    "additionalProperties": False,
}


class _Wire:
    def __init__(self, provider, *responses):
        endpoint, self.path = _ENDPOINTS[provider]
        self.identity = ProviderIdentity(provider, _MODEL, endpoint, "practice-account")
        self.requests = []
        self.responses = iter(responses)
        self.transport = httpx.MockTransport(self.send)

    def send(self, request):
        self.requests.append(request)
        response = next(self.responses)
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json=response)

    def bodies(self):
        for request in self.requests:
            assert request.method == "POST"
            assert request.url.host == httpx.URL(self.identity.endpoint).host
            assert request.url.path == self.path
            if self.identity.provider == "gemini":
                assert request.headers["x-goog-api-key"] == _KEY
            elif self.identity.provider == "openai":
                assert request.headers["authorization"] == f"Bearer {_KEY}"
                assert "openai-organization" not in request.headers
                assert "openai-project" not in request.headers
            else:
                assert request.headers["x-api-key"] == _KEY
                assert "authorization" not in request.headers
            yield json.loads(request.content)


def _call(provider, identifier, arguments=_ARGUMENTS):
    if provider == "gemini":
        return {
            "functionCall": {"id": identifier, "name": "open_section", "args": arguments},
            "thoughtSignature": "cHJhY3RpY2Utc2lnbmF0dXJl",
        }
    if provider == "openai":
        return {"type": "function_call", "id": f"fc_{identifier}", "status": "completed",
                "call_id": identifier, "name": "open_section",
                "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments)}
    return {"type": "tool_use", "id": identifier, "name": "open_section", "input": arguments}


def _text(provider, text):
    if provider == "gemini":
        return {"text": text}
    if provider == "openai":
        return {"type": "message", "role": "assistant", "id": "msg_1", "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}]}
    return {"type": "text", "text": text}


def _response(provider, *output, tool=True):
    if provider == "gemini":
        return {"candidates": [{"index": 0, "finishReason": "STOP", "content": {"role": "model", "parts": output}}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 3, "thoughtsTokenCount": 2}}
    if provider == "openai":
        return {"id": "resp_1", "model": _MODEL, "object": "response", "status": "completed", "created_at": 10,
                "output": output, "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
                "usage": {"input_tokens": 10, "input_tokens_details": {"cached_tokens": 0},
                          "output_tokens": 5, "output_tokens_details": {"reasoning_tokens": 2}, "total_tokens": 15}}
    return {"type": "message", "id": "msg_1", "model": _MODEL, "role": "assistant", "content": output,
            "stop_reason": "tool_use" if tool else "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 6, "cache_read_input_tokens": 3,
                      "cache_creation_input_tokens": 1, "output_tokens": 5}}


@pytest.mark.parametrize("provider", _ENDPOINTS)
def test_sdk_tool_proposal_and_result_roundtrip(provider, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "ignored-env-gemini")
    monkeypatch.setenv("GOOGLE_GENAI_CLIENT_MODE", "replay")
    monkeypatch.setenv("OPENAI_API_KEY", "ignored-env-openai")
    monkeypatch.setenv("OPENAI_ORG_ID", "ignored-env-org")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "ignored-env-project")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ignored-env-anthropic")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ignored-env-bearer")
    first = _call(provider, "call_1")
    second_args = {"observationId": "obs-2", "controlRef": "sec-2"}
    prefix = [{"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque-reasoning",
               "summary": [{"type": "summary_text", "text": "discard-this-summary"}]}] if provider == "openai" else []
    second = _response(provider, _call(provider, "call_2", second_args))
    if provider == "gemini":
        second["usageMetadata"].update(toolUsePromptTokenCount=4, totalTokenCount=19)
    wire = _Wire(provider, _response(provider, *prefix, first), second)
    before = {"observationId": "obs-1"}
    after = {"observationId": "obs-2"}
    result = {"section": "synthetic-wages"}

    async def run():
        async with open_provider(wire.identity, api_key=_KEY, transport=wire.transport) as adapter:
            assert adapter.identity == wire.identity
            with pytest.raises(FrozenInstanceError):
                adapter.identity.account = "changed-account"
            proposed = await adapter.propose(before, _TOOLS, None, **_LIMITS)
            assert proposed == ProviderTurn((ToolCall("call_1", "open_section", _ARGUMENTS),), Usage(10, 5))
            next_turn = await adapter.propose(after, _TOOLS, ToolFeedback(proposed.calls[0], result), **_LIMITS)
            assert next_turn == ProviderTurn(
                (ToolCall("call_2", "open_section", second_args),), Usage(14 if provider == "gemini" else 10, 5),
            )
        with pytest.raises(ProviderError, match="closed"):
            await adapter.propose(before, _TOOLS, None, **_LIMITS)

    asyncio.run(run())
    a, b = wire.bodies()
    if provider == "gemini":
        assert a["tools"][0]["functionDeclarations"] == [{"name": "open_section", "description": _TOOLS[0].description,
                                                           "parameters_json_schema": _PARAMETERS}]
        assert a["generationConfig"]["maxOutputTokens"] == 128
        assert a["toolConfig"]["functionCallingConfig"]["mode"] == "ANY"
        assert json.loads(a["contents"][0]["parts"][0]["text"]) == before
        previous, model, feedback, current = b["contents"]
        assert previous == a["contents"][0]
        assert model == {"role": "model", "parts": [first]}
        assert feedback["parts"][0]["functionResponse"] == {
            "name": "open_section", "id": "call_1", "response": {"output": result},
        }
        assert json.loads(current["parts"][0]["text"]) == after
    elif provider == "openai":
        assert a["tools"] == [{"type": "function", "name": "open_section", "description": _TOOLS[0].description,
                               "parameters": _PARAMETERS, "strict": False}]
        assert a["max_output_tokens"] == 128 and a["model"] == _MODEL
        assert a["tool_choice"] == "required"
        assert a["parallel_tool_calls"] is False
        assert a["store"] is b["store"] is False
        assert "previous_response_id" not in b
        assert a["include"] == ["reasoning.encrypted_content"]
        previous, reasoning, native_call, feedback, current = b["input"]
        assert json.loads(previous["content"]) == before
        assert reasoning == {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque-reasoning"}
        assert native_call == first
        assert feedback["type"] == "function_call_output" and feedback["call_id"] == "call_1"
        assert json.loads(feedback["output"]) == result
        assert json.loads(current["content"]) == after
    else:
        assert a["tools"] == [{"name": "open_section", "description": _TOOLS[0].description, "input_schema": _PARAMETERS}]
        assert a["max_tokens"] == 128 and a["model"] == _MODEL
        assert a["tool_choice"] == {"type": "any", "disable_parallel_tool_use": True}
        previous, assistant, feedback, current = b["messages"]
        assert json.loads(previous["content"]) == before
        assert assistant == {"role": "assistant", "content": [first]}
        assert feedback["content"][0]["type"] == "tool_result" and feedback["content"][0]["tool_use_id"] == "call_1"
        assert json.loads(feedback["content"][0]["content"]) == result
        assert json.loads(current["content"]) == after


@pytest.mark.parametrize("provider", _ENDPOINTS)
def test_sdk_structured_extraction_accepts_only_text(provider):
    text = "1: Synthetic wages 0.00"
    facts = {"facts": ["0.00"]}
    wire = _Wire(provider, _response(provider, _text(provider, json.dumps(facts)), tool=False))

    async def run():
        async with open_provider(wire.identity, api_key=_KEY, transport=wire.transport) as adapter:
            with pytest.raises(ProviderError, match="text only"):
                await adapter.extract("Extract facts.", b"image", _FACTS_SCHEMA, **_LIMITS)
            assert await adapter.extract("Extract facts.", text, _FACTS_SCHEMA, **_LIMITS) == (facts, Usage(10, 5))

    asyncio.run(run())
    sent, = wire.bodies()
    assert "tools" not in sent
    if provider == "gemini":
        assert sent["contents"] == [{"role": "user", "parts": [{"text": text}]}]
        assert "Extract facts." in sent["systemInstruction"]["parts"][0]["text"]
        assert sent["generationConfig"]["responseJsonSchema"] == _FACTS_SCHEMA
        assert sent["generationConfig"]["responseMimeType"] == "application/json"
    elif provider == "openai":
        assert sent["input"] == [{"role": "user", "content": text}]
        assert "Extract facts." in sent["instructions"]
        assert sent["text"]["format"] == {"type": "json_schema", "name": "tax_facts", "strict": True, "schema": _FACTS_SCHEMA}
        assert sent["store"] is False
    else:
        assert sent["messages"] == [{"role": "user", "content": text}]
        assert "Extract facts." in sent["system"]
        assert sent["output_config"] == {"format": {"type": "json_schema", "schema": _FACTS_SCHEMA}}


@pytest.mark.parametrize("provider", _ENDPOINTS)
def test_multiple_calls_cannot_be_continued_as_one(provider):
    wire = _Wire(provider, _response(provider, _call(provider, "call_A"), _call(provider, "call_B")))

    async def run():
        async with open_provider(wire.identity, api_key=_KEY, transport=wire.transport) as adapter:
            turn = await adapter.propose({}, _TOOLS, None, **_LIMITS)
            assert [call.call_id for call in turn.calls] == ["call_A", "call_B"]
            with pytest.raises(ProviderError, match="preceding single proposal"):
                await adapter.propose({}, _TOOLS, ToolFeedback(turn.calls[0], {}), **_LIMITS)

    asyncio.run(run())
    assert len(wire.requests) == 1


def test_gemini_missing_native_id_gets_local_correlation_only():
    first = _call("gemini", None)
    first["functionCall"].pop("id")
    wire = _Wire("gemini", _response("gemini", first), _response("gemini", _text("gemini", "No action")))

    async def run():
        async with open_provider(wire.identity, api_key=_KEY, transport=wire.transport) as adapter:
            proposal = await adapter.propose({}, _TOOLS, None, **_LIMITS)
            call, = proposal.calls
            assert call.call_id
            turn = await adapter.propose({}, _TOOLS, ToolFeedback(call, {}), **_LIMITS)
            assert turn.calls == ()

    asyncio.run(run())
    _, sent = wire.bodies()
    assert sent["contents"][1]["parts"][0] == first
    assert "id" not in sent["contents"][2]["parts"][0]["functionResponse"]


@pytest.mark.parametrize("arguments", ['["array-not-object"]', '{"duplicate":1,"duplicate":2}', '{"invalid":NaN}'])
def test_openai_rejects_malformed_tool_arguments(arguments):
    wire = _Wire("openai", _response("openai", _call("openai", "call_1", arguments)))

    async def run():
        async with open_provider(wire.identity, api_key=_KEY, transport=wire.transport) as adapter:
            with pytest.raises(ProviderError, match="valid JSON object"):
                await adapter.propose({}, _TOOLS, None, **_LIMITS)

    asyncio.run(run())
    assert len(wire.requests) == 1


def test_gemini_503_preserves_previous_tool_feedback_for_retry():
    unavailable = httpx.Response(503, json={"error": {"code": 503, "status": "UNAVAILABLE", "message": "Busy"}})
    wire = _Wire("gemini", _response("gemini", _call("gemini", "call_1")), unavailable,
                 _response("gemini", _call("gemini", "call_2")))

    async def run():
        async with open_provider(wire.identity, api_key=_KEY, transport=wire.transport) as adapter:
            initial = await adapter.propose({}, _TOOLS, None, **_LIMITS)
            feedback = ToolFeedback(initial.calls[0], {"status": "applied"})
            with pytest.raises(ProviderError) as failure:
                await adapter.propose({}, _TOOLS, feedback, **_LIMITS)
            assert failure.value.status == 503
            retried = await adapter.propose({}, _TOOLS, feedback, **_LIMITS)
            assert retried.calls[0].call_id == "call_2"

    asyncio.run(run())
    _, failed, retried = wire.bodies()
    assert failed["contents"] == retried["contents"]


@pytest.mark.parametrize("provider", _ENDPOINTS)
@pytest.mark.parametrize("status", [307, 503])
def test_transport_neither_retries_nor_follows_redirects_and_sanitizes_errors(provider, status):
    sensitive = "a-provider-echo-of-sensitive-source"
    headers = {"Location": "https://different-recipient.test/v1", "Retry-After": "0"}
    body = {"error": {"code": status, "message": sensitive, "status": "UNAVAILABLE"}}
    wire = _Wire(provider, httpx.Response(status, headers=headers, json=body))

    async def run():
        async with open_provider(wire.identity, api_key=_KEY, transport=wire.transport) as adapter:
            with pytest.raises(ProviderError) as exc:
                await adapter.propose({}, _TOOLS, None, **_LIMITS)
            assert sensitive not in str(exc.value)
            assert _KEY not in str(exc.value)
            assert exc.value.__context__ is None or exc.value.__suppress_context__

    asyncio.run(run())
    assert len(wire.requests) == 1


def test_environment_cannot_supply_or_override_the_approved_secret(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "not-approved")
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "Authorization: Bearer not-approved")
    wire = _Wire("openai")

    async def run():
        with pytest.raises(ProviderError, match="explicit API key"):
            async with open_provider(wire.identity, api_key="", transport=wire.transport):
                pytest.fail("Opened without an explicit key")
        with pytest.raises(ProviderError, match="environment override"):
            async with open_provider(wire.identity, api_key=_KEY, transport=wire.transport):
                pytest.fail("Opened with an unauthorized header override")

    asyncio.run(run())
    assert not wire.requests


def test_changed_feedback_arguments_are_rejected_without_a_request():
    wire = _Wire("anthropic", _response("anthropic", _call("anthropic", "call_1")))

    async def run():
        async with open_provider(wire.identity, api_key=_KEY, transport=wire.transport) as adapter:
            turn = await adapter.propose({}, _TOOLS, None, **_LIMITS)
            call, = turn.calls
            changed = ToolCall(call.call_id, call.name, {"observationId": "foreign", "controlRef": "sec-1"})
            with pytest.raises(ProviderError, match="preceding single proposal"):
                await adapter.propose({}, _TOOLS, ToolFeedback(changed, {}), **_LIMITS)

    asyncio.run(run())
    assert len(wire.requests) == 1


def test_gemini_rejects_mixed_text_and_image_parts():
    unsolicited_image = {"text": "unexpected", "inlineData": {"mimeType": "image/png", "data": "eA=="}}
    wire = _Wire("gemini", _response("gemini", _call("gemini", "call_1"), unsolicited_image))

    async def run():
        async with open_provider(wire.identity, api_key=_KEY, transport=wire.transport) as adapter:
            with pytest.raises(ProviderError, match="outside the permitted text"):
                await adapter.propose({}, _TOOLS, None, **_LIMITS)

    asyncio.run(run())
    assert len(wire.requests) == 1


@pytest.mark.parametrize("provider", _ENDPOINTS)
def test_extraction_wire_limit_and_disclosure_hook_order(provider):
    text = '1: Café "\\quoted"\n2: 零' * 15
    facts = {"facts": ["0.00"]}
    response = _response(provider, _text(provider, json.dumps(facts)), tool=False)
    baseline = _Wire(provider, response)
    limited = _Wire(provider, response)
    recorded = []

    async def record_attempt():
        assert limited.requests == []
        recorded.append("before dispatch")

    async def run():
        async with open_provider(baseline.identity, api_key=_KEY, transport=baseline.transport) as adapter:
            await adapter.extract("Extract.", text, _FACTS_SCHEMA, **_LIMITS)
        body_length = len(baseline.requests[0].content)
        async with open_provider(limited.identity, api_key=_KEY, transport=limited.transport) as adapter:
            with pytest.raises(ProviderRequestTooLarge, match="serialized SDK request"):
                await adapter.extract(
                    "Extract.", text, _FACTS_SCHEMA, before_request=record_attempt,
                    max_output_tokens=128, max_request_bytes=body_length - 1,
                )
            assert not recorded and not limited.requests
            await adapter.extract(
                "Extract.", text, _FACTS_SCHEMA, before_request=record_attempt,
                max_output_tokens=128, max_request_bytes=body_length,
            )

    asyncio.run(run())
    assert recorded == ["before dispatch"]
    assert [request.content for request in limited.requests] == [baseline.requests[0].content]


@pytest.mark.parametrize("provider", _ENDPOINTS)
def test_disclosure_hook_failure_blocks_sdk_and_preserves_local_issue(provider):
    class LeaseLost(Exception):
        pass

    wire = _Wire(provider)
    calls = []

    async def refuse():
        calls.append("refused")
        raise LeaseLost

    async def run():
        async with open_provider(wire.identity, api_key=_KEY, transport=wire.transport) as adapter:
            with pytest.raises(LeaseLost):
                await adapter.extract("Extract.", "synthetic", _FACTS_SCHEMA, before_request=refuse, **_LIMITS)

    asyncio.run(run())
    assert calls == ["refused"]
    assert wire.requests == []


@pytest.mark.parametrize("provider", _ENDPOINTS)
def test_continuation_limit_covers_previous_observation_and_native_parts(provider):
    first_call = _call(provider, "call_1")
    prefix = [{"type": "reasoning", "id": "reasoning_1", "summary": [],
               "encrypted_content": "opaque" * 60}] if provider == "openai" else []
    first = _response(provider, *prefix, first_call)
    second = _response(provider, _call(provider, "call_2"))
    baseline = _Wire(provider, first, second)
    limited = _Wire(provider, first)
    prior = {"observationId": "obs-1", "choices": ["é\"\\"] * 120}
    current = {"observationId": "obs-2"}

    async def continue_turn(wire, byte_limit=None):
        async with open_provider(wire.identity, api_key=_KEY, transport=wire.transport) as adapter:
            turn = await adapter.propose(prior, _TOOLS, None, **_LIMITS)
            feedback = ToolFeedback(turn.calls[0], {"status": "applied"})
            if byte_limit is None:
                await adapter.propose(current, _TOOLS, feedback, **_LIMITS)
            else:
                with pytest.raises(ProviderRequestTooLarge):
                    await adapter.propose(current, _TOOLS, feedback, max_output_tokens=128, max_request_bytes=byte_limit)

    asyncio.run(continue_turn(baseline))
    oversized = len(baseline.requests[1].content)
    assert oversized > len(baseline.requests[0].content)
    asyncio.run(continue_turn(limited, oversized - 1))
    assert len(limited.requests) == 1
