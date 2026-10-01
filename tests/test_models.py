import json
from functools import partial

import httpx
from dynamic_graph import ModelRequest
from langchain_deepseek import ChatDeepSeek

import karen.models
from karen.intent.recognizer import Assessment
from karen.models import deepseek_client


async def test_deepseek_backend_structured_response_over_mock_http(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = []
    payload = {"decision": {"outcome": "needs_clarification", "questions": ["给谁写？"]}}

    async def send(request):
        calls.append(json.loads(request.content))
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
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-test",
                                    "type": "function",
                                    "function": {
                                        "name": "StructuredResponse",
                                        "arguments": json.dumps(payload, ensure_ascii=False),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http_client:
        monkeypatch.setattr(
            karen.models, "ChatDeepSeek", partial(ChatDeepSeek, http_async_client=http_client)
        )
        response = await deepseek_client().generate(
            ModelRequest(
                role="intent",
                system_instruction="判断请求是否清晰",
                task_instruction="返回澄清问题",
                input_data={"user_input": "写邮件"},
                output_schema=Assessment.model_json_schema(),
            )
        )
    assert response.payload == payload
    assert response.usage == {"input_tokens": 10, "output_tokens": 5}
    assert len(calls) == 1
    assert calls[0]["model"] == "deepseek-chat"
    assert calls[0]["tools"][0]["function"]["parameters"]["type"] == "object"
    assert "写邮件" in calls[0]["messages"][-1]["content"]
