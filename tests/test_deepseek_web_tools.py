import time
from types import SimpleNamespace

import httpx
import pytest
from dynamic_graph.execution.errors import ToolCallError
from dynamic_graph.tools import tavily_search_tool
from jsonschema import validate

from karen.tools.deepseek_web import deepseek_fetch_search_results_tool, deepseek_web_tools


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


async def test_read_search_results_accepts_empty_and_preserves_other_pages_after_failure():
    import json

    from dynamic_graph.graph.schemas import compatible

    urls = [f"https://example.com/{index}" for index in range(4)]
    calls = []

    def respond(request):
        url = json.loads(request.content)["url"]
        calls.append(url)
        if url == urls[1]:
            return httpx.Response(404)
        return httpx.Response(200, json={"title": "Report", "content": f"Full text for {url}"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        search, fetch = deepseek_web_tools(client, "test-key")
        batch = deepseek_fetch_search_results_tool(search, fetch)
        for provider in (search, tavily_search_tool(api_key="test-key")):
            assert compatible(
                provider.output_schema["properties"]["results"],
                batch.input_schema["properties"]["results"],
            )
        assert await batch.handler({"results": []}, context()) == {"pages": []}
        assert not calls
        data = {
            "results": [
                {"title": "", "url": url, "content": "Summary", "score": 1}
                for url in [urls[0], *urls]
            ],
        }
        validate(data, batch.input_schema)
        result = await batch.handler(data, context())
    validate(result, batch.output_schema)
    assert set(calls) == set(urls[:3]) and len(calls) == 3
    assert [page["url"] for page in result["pages"]] == urls[:3]
    assert result["pages"][1]["error"] == "TOOL_HTTP_ERROR"
    assert result["pages"][1]["content"] == ""
    assert result["pages"][2]["content"] == f"Full text for {urls[2]}"


async def test_followup_search_skips_sufficient_evidence_and_reports_failed_lookup():
    from karen.tools.deepseek_web import deepseek_search_followup_tool

    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={"results": []})
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        search, _ = deepseek_web_tools(client, "test-key")
        tool = deepseek_search_followup_tool(search)
        skip = {"query": "   "}
        validate(skip, tool.input_schema)
        assert await tool.handler(skip, context()) == {"query": "", "results": []}
        assert not requests
        query = {"query": "outage mitigation timeline"}
        result = await tool.handler(query, context())
        validate(result, tool.output_schema)
        assert result == {**query, "results": []}
        failed = await tool.handler(query, context())
        validate(failed, tool.output_schema)
        assert failed == {**query, "results": [], "search_error": "TOOL_HTTP_ERROR"}
        assert len(requests) == 2
