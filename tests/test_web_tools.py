import time
from types import SimpleNamespace

from dynamic_graph import DynamicGraphEngine, FakeModelClient, ModelBindings

from karen.tools.web import extract_html, web_read_page_tool


def test_extract_main_preserves_evidence_and_discards_noise():
    result = extract_html(
        """<html><head><title>Example &amp; title</title><script>secret script</script></head>
    <body><header>Site banner</header><nav>Navigation</nav><main><article><header><h1>Heading</h1></header>
    <p>Useful <a href="/repo">repository</a></p><ul><li>Item</li></ul>
    <table><tr><th>Name</th><th>Count</th></tr><tr><td>Test</td><td>15</td></tr></table>
    <svg><path d="huge path"/>icon noise</svg><p hidden>hidden noise</p>
    <p aria-hidden="true">aria noise</p><style>CSS noise</style></article></main>
    <aside>Outside main</aside><footer>Footer noise</footer></body></html>""",
        "https://example.test/page",
    )
    assert result["title"] == "Example & title"
    text = result["content"]
    assert "# Heading" in text and "https://example.test/repo" in text
    assert "repository" in text and "- Item" in text and "| Test | 15" in text
    for noise in [
        "secret script",
        "Site banner",
        "Navigation",
        "icon noise",
        "hidden noise",
        "aria noise",
        "CSS noise",
        "Outside main",
        "Footer noise",
    ]:
        assert noise not in text
    assert not result["truncated"] and result["extraction_scope"] == "main"


def test_fallback_and_utf8_truncation_are_explicit():
    result = extract_html(
        "<p>正文内容</p><script>noise</script>", "https://example.test", max_bytes=7
    )
    assert result["content"] == "正文"
    assert result["truncated"] and result["extracted_bytes"] == 12
    assert result["extraction_scope"] == "document_without_boilerplate"
    assert (
        "javascript:"
        not in extract_html('<a href="javascript:evil()">label</a>', "https://example.test")[
            "content"
        ]
    )
    assert "Text" in extract_html("<main></main><p>Text</p>", "https://example.test")["content"]


async def test_tool_registered_and_fetch_metadata_preserved(monkeypatch):
    import karen.tools.web as web

    async def fetch(data, context):
        assert data == {"url": "https://example.test"}
        return {
            "url": "https://example.test/final",
            "content_type": "text/html",
            "fetched_at": "2026-11-05T00:00:00Z",
            "content": '<main><a href="/a">A</a></main>',
        }

    original = web.web_fetch_tool()
    monkeypatch.setattr(
        web,
        "web_fetch_tool",
        lambda: SimpleNamespace(
            handler=fetch,
            input_schema=original.input_schema,
            output_schema=original.output_schema,
        ),
    )
    tool = web_read_page_tool()
    engine = DynamicGraphEngine(models=ModelBindings(FakeModelClient(), FakeModelClient()))
    engine.register_tool(tool)
    assert engine.get_capability("web.read_page", "1.0.0")
    result = await tool.handler(
        {"url": "https://example.test"}, SimpleNamespace(deadline=time.monotonic() + 10)
    )
    assert result["url"].endswith("/final") and result["fetched_at"] == "2026-11-05T00:00:00Z"
    assert "https://example.test/a" in result["content"]
    from jsonschema import validate

    validate(result, tool.output_schema)
