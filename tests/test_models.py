import json
from functools import partial

import httpx
import pytest
from dynamic_graph import ModelRequest
from dynamic_graph.models.client import ModelCallError
from langchain_deepseek import ChatDeepSeek

import karen.models
from karen.intent.recognizer import Assessment
from karen.models import deepseek_client


@pytest.mark.parametrize(
    "mode,surplus_delimiter",
    [("json_mode", False), ("json_mode", True), ("function_calling", False)],
)
async def test_deepseek_backend_json_object_request_retains_strict_parsing(
    monkeypatch, mode, surplus_delimiter
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = []
    payload = {"decision": {"outcome": "needs_clarification", "questions": ["给谁写？"]}}

    async def send(request):
        calls.append(json.loads(request.content))
        message = {
            "role": "assistant",
            "content": json.dumps(payload, ensure_ascii=False) + ("}" if surplus_delimiter else ""),
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
            karen.models, "ChatDeepSeek", partial(ChatDeepSeek, http_async_client=http_client)
        )
        client = deepseek_client(mode=mode)
        task = ModelRequest(
            role="intent",
            system_instruction="判断请求是否清晰",
            task_instruction="返回澄清问题",
            input_data={"user_input": "写邮件"},
            output_schema=Assessment.model_json_schema(),
        )
        if surplus_delimiter:
            with pytest.raises(ModelCallError) as caught:
                await client.generate(task)
            assert caught.value.code == "MODEL_RESPONSE_INVALID"
            assert caught.value.raw_response.endswith("}")
        else:
            response = await client.generate(task)
            assert response.payload == payload
            assert response.usage == {"input_tokens": 10, "output_tokens": 5}
    assert len(calls) == 1
    assert calls[0]["model"] == "deepseek-chat"
    if mode == "json_mode":
        assert calls[0]["response_format"] == {"type": "json_object"}
        assert "tools" not in calls[0]
        assert "schema" in calls[0]["messages"][-1]["content"]
    else:
        assert "response_format" not in calls[0]
        assert calls[0]["tools"][0]["function"]["parameters"]["type"] == "object"
    assert any("写邮件" in message["content"] for message in calls[0]["messages"])
