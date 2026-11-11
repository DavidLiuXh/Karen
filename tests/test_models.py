import json
from functools import partial

import httpx
import pytest
from dynamic_graph import ModelRequest
from dynamic_graph.models.client import ModelCallError
from langchain_deepseek import ChatDeepSeek

import karen.model.models
from karen.intent.recognizer import Assessment
from karen.model.models import deepseek_client


@pytest.mark.parametrize(
    "mode,surplus_delimiter",
    [("json_mode", False), ("json_mode", True), ("function_calling", False)],
)
@pytest.mark.parametrize("thinking", [False, True])
async def test_deepseek_backend_json_object_request_retains_strict_parsing(
    monkeypatch, mode, surplus_delimiter, thinking
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = []
    payload = {"decision": {"outcome": "needs_clarification", "questions": ["给谁写？"]}}

    async def send(request):
        calls.append(json.loads(request.content))
        message = {
            "role": "assistant",
            "content": json.dumps(payload, ensure_ascii=False) + ("}" if surplus_delimiter else ""),
            "reasoning_content": "private_thinking_content",
        }
        if mode == "function_calling":
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "test-call",
                        "type": "function",
                        "function": {
                            "name": "StructuredResponse",
                            "arguments": json.dumps(payload, ensure_ascii=False),
                        },
                    }
                ],
            }
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "completion-test",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-chat",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls" if mode == "function_calling" else "stop",
                        "message": message,
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http_client:
        monkeypatch.setattr(
            karen.model.models, "ChatDeepSeek", partial(ChatDeepSeek, http_async_client=http_client)
        )
        client = deepseek_client(mode=mode, thinking=thinking)
        task = ModelRequest(
            role="intent",
            system_instruction="判断请求是否清晰",
            task_instruction="返回澄清问题",
            input_data={"user_input": "写邮件"},
            output_schema=Assessment.model_json_schema(),
            max_output_tokens=29,
        )
        if surplus_delimiter:
            with pytest.raises(ModelCallError) as caught:
                await client.generate(task)
            assert caught.value.code == "MODEL_RESPONSE_INVALID"
            assert caught.value.raw_response.endswith("}")
        else:
            response = await client.generate(task)
            assert response.payload == payload
            assert "private_thinking_content" not in json.dumps(response.payload)
            assert response.usage == {"input_tokens": 10, "output_tokens": 5}
    assert len(calls) == 1
    assert calls[0]["model"] == "deepseek-flash"
    assert calls[0]["max_tokens"] == 29
    assert client.metadata["thinking"] is thinking
    if thinking:
        assert calls[0]["thinking"] == {"type": "enabled"}
        assert calls[0]["reasoning_effort"] == "low"
    else:
        assert calls[0]["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" not in calls[0]
    if mode == "json_mode":
        assert calls[0]["response_format"] == {"type": "json_object"}
        assert "tools" not in calls[0]
        assert "schema" in calls[0]["messages"][-1]["content"]
    else:
        assert "response_format" not in calls[0]
        assert calls[0]["tools"][0]["function"]["parameters"]["type"] == "object"
    assert any("写邮件" in message["content"] for message in calls[0]["messages"])


async def test_frozen_grader_http_request_keeps_legacy_generation_parameters(monkeypatch):
    from karen.evaluation import runner
    from karen.evaluation.prompts import JUDGE_SCHEMA

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = []

    async def send(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, request=request, json={
            "id": "test", "object": "chat.completion", "created": 1, "model": "deepseek-flash",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{"id": "test", "type": "function", "function": {
                    "name": "StructuredResponse", "arguments": '{"passed":true,"reason":"符合参考"}'
                }}],
            }}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http_client:
        monkeypatch.setattr(runner, "ChatDeepSeek", partial(ChatDeepSeek, http_async_client=http_client))
        client = runner.frozen_judge_client()
        response = await client.generate(ModelRequest(
            role="evaluation_judge", system_instruction="Grade", task_instruction="Return JSON",
            input_data={}, output_schema=JUDGE_SCHEMA, max_output_tokens=1024,
        ))
    assert response.payload["passed"] is True
    assert calls[0]["model"] == "deepseek-chat" and calls[0]["temperature"] == 0
    assert "max_tokens" not in calls[0] and "max_completion_tokens" not in calls[0]
    assert "thinking" not in calls[0] and "reasoning_effort" not in calls[0]
    assert "response_format" not in calls[0] and calls[0]["tools"]
    assert client.metadata["output_budget_enforced"] is False


@pytest.mark.parametrize("effort,effective", [("low", "low"), ("medium", "high")])
async def test_truncation_recovery_changes_actual_http_budget_once(monkeypatch, effort, effective):
    from karen.intent import IntentRecognizer

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = []
    payload = {"decision": {"outcome": "needs_clarification", "questions": ["请提供收件人。"]}}

    async def send(request):
        calls.append(json.loads(request.content))
        truncated = len(calls) == 1
        return httpx.Response(200, request=request, json={
            "id": "completion-recovery", "object": "chat.completion", "created": 1,
            "model": "deepseek-flash", "choices": [{"index": 0,
                "finish_reason": "length" if truncated else "stop",
                "message": {"role": "assistant", "content": '{"decision":' if truncated else json.dumps(payload),
                            "reasoning_content": "internal"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http_client:
        monkeypatch.setattr(karen.model.models, "ChatDeepSeek", partial(ChatDeepSeek, http_async_client=http_client))
        client = deepseek_client(reasoning_effort=effort)
        task = ModelRequest(role="clarity", system_instruction="Assess clarity",
                            task_instruction="Return the required JSON", input_data={"text": "写邮件"},
                            output_schema=Assessment.model_json_schema())
        result = await IntentRecognizer(client)._validated(task, Assessment)
        assert result.decision.outcome == "needs_clarification"
        # A later independent request must retain the initial default.
        assert (await client.generate(task)).payload == payload
    assert [call["max_tokens"] for call in calls] == [16384, 32768, 16384]
    assert all(call["thinking"] == {"type": "enabled"} for call in calls)
    assert all(call["reasoning_effort"] == effort for call in calls)
    assert client.metadata["reasoning_effort"] == effort
    assert client.metadata["effective_reasoning_effort"] == effective


async def test_rerank_sends_32768_and_does_not_retry_truncation(monkeypatch):
    import time
    from types import SimpleNamespace

    from karen.context.contracts import Ranking
    from karen.context.retrieval import Retriever
    from karen.observability import Observer

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = []

    async def send(request):
        calls.append(json.loads(request.content))
        truncated = len(calls) == 1
        return httpx.Response(200, request=request, json={
            "id": "rerank-budget", "object": "chat.completion", "created": 1,
            "model": "deepseek-flash", "choices": [{"index": 0,
                "finish_reason": "length" if truncated else "stop",
                "message": {"role": "assistant", "content": '{"ranking":' if truncated
                            else '{"ranking": [], "history_status": "none"}'}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http:
        monkeypatch.setattr(karen.model.models, "ChatDeepSeek", partial(ChatDeepSeek, http_async_client=http))
        client = deepseek_client(reasoning_effort="low")
        retriever = Retriever(SimpleNamespace(
            model=client, rerank_model=client, storage=None, observer=Observer(),
        ))
        with pytest.raises(ModelCallError) as failed:
            await retriever.call({"deadline": time.monotonic() + 180}, "memory_rerank",
                                 "Rank the synthetic candidates", {}, Ranking)
        assert failed.value.code == "MODEL_RESPONSE_TRUNCATED"
        assert len(calls) == 1
        result = await retriever.call({"deadline": time.monotonic() + 180}, "memory_rerank",
                                      "Rank the synthetic candidates", {}, Ranking)
        assert result.ranking == []
    assert [call["max_tokens"] for call in calls] == [32768, 32768]
    assert all(call["thinking"] == {"type": "enabled"} for call in calls)
    assert all(call["reasoning_effort"] == "low" for call in calls)


@pytest.mark.parametrize("catalog_status", [200, 503])
async def test_execution_output_limits_follow_selected_provider_models(monkeypatch, catalog_status):
    from karen.model.models import configure_execution_output_limits

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    requests = []
    catalog = {
        "object": "list",
        "data": [
            {"id": "deepseek-flash", "object": "model", "created": 1,
             "owned_by": "deepseek", "max_output_tokens": 393216},
            {"id": "deepseek-v4-pro", "object": "model", "created": 1,
             "owned_by": "deepseek", "max_output_tokens": 65536},
        ],
    }

    async def send(request):
        if request.url.path.endswith("/models"):
            requests.append("catalog")
            return httpx.Response(catalog_status, json=catalog)
        body = json.loads(request.content)
        requests.append((body["model"], body["max_tokens"]))
        return httpx.Response(200, json={
            "id": "completion", "object": "chat.completion", "created": 1,
            "model": body["model"], "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": '{"answer":"ok"}'}}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http_client:
        monkeypatch.setattr(
            karen.model.models, "ChatDeepSeek", partial(ChatDeepSeek, http_async_client=http_client)
        )
        planner = deepseek_client(model="deepseek-v4-pro")
        worker = deepseek_client(model="deepseek-flash")
        limit = await configure_execution_output_limits(planner, worker)
        for role, client in [("planner", planner), ("worker", worker)]:
            await client.generate(ModelRequest(
                role=role, system_instruction="Return JSON", task_instruction="Return an answer",
                input_data={}, output_schema={"type": "object", "properties": {
                    "answer": {"type": "string"}}, "required": ["answer"]},
                max_output_tokens=limit,
            ))

    planner_limit, worker_limit = (65536, 393216) if catalog_status == 200 else (32768, 32768)
    assert requests == ["catalog", ("deepseek-v4-pro", planner_limit), ("deepseek-flash", worker_limit)]
    assert worker.metadata["output_token_limit_source"] == (
        "provider" if catalog_status == 200 else "default"
    )
