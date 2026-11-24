import time
from types import SimpleNamespace

import httpx
import pytest
from dynamic_graph.execution.errors import ToolCallError
from dynamic_graph.tools import tavily_search_tool
from jsonschema import validate

from karen.tools.deepseek_web import deepseek_web_tools


def context():
    return SimpleNamespace(deadline=time.monotonic() + 5)


async def test_search_maps_deepseek_to_tavily_result_contract():
    def respond(request):
        assert request.method == "POST"
        assert str(request.url) == "https://api.deepseek.com/v1/tools/search"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert request.content == b'{"query":"Karen site:github.com","count":2}'
        return httpx.Response(200, json={"results": [{
            "title": "Karen", "url": "https://github.com/example/karen",
            "summary": "", "snippet": "Project", "rerank_score": 2.4,
        }]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        search, _ = deepseek_web_tools(client, "test-key")
        data = {"query": "Karen site:github.com", "max_results": 2}
        validate(data, search.input_schema)
        result = await search.handler(data, context())
    validate(result, search.output_schema)
    validate(result, tavily_search_tool(api_key="test-key").output_schema)
    assert result == {"query": data["query"], "results": [{
        "title": "Karen", "url": "https://github.com/example/karen",
        "content": "Project", "score": 2.4,
    }]}


async def test_fetch_preserves_markdown_and_limits_utf8_output():
    url = "https://example.com/page"
    markdown = "# Title\n" + "正文" * 5000

    def respond(request):
        assert str(request.url) == "https://api.deepseek.com/v1/tools/fetch"
        assert request.content == b'{"url":"https://example.com/page"}'
        return httpx.Response(200, json={"title": "Title", "content": markdown})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        _, fetch = deepseek_web_tools(client, "test-key")
        result = await fetch.handler({"url": url}, context())
        with pytest.raises(ToolCallError, match="no credentials"):
            await fetch.handler({"url": "https://name:password@example.com"}, context())
    validate(result, fetch.output_schema)
    assert result["title"] == "Title" and result["url"] == url
    assert result["truncated"] and result["extracted_bytes"] == len(markdown.encode())
    assert len(result["content"].encode()) <= 24_000
    assert markdown.startswith(result["content"])


@pytest.mark.parametrize(("status", "retryable"), [(402, False), (403, False), (429, True)])
async def test_provider_errors_preserve_status_without_exposing_response(status, retryable):
    transport = httpx.MockTransport(lambda _: httpx.Response(status, text="private body"))
    async with httpx.AsyncClient(transport=transport) as client:
        _, fetch = deepseek_web_tools(client, "test-key")
        with pytest.raises(ToolCallError, match=f"DeepSeek fetch HTTP {status}") as error:
            await fetch.handler({"url": "https://example.com"}, context())
    assert error.value.details == {"http_status": status}
    assert error.value.retryable is retryable
