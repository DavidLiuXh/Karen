import time
from types import SimpleNamespace

import pytest
from dynamic_graph import CancellationToken
from dynamic_graph.execution.errors import ToolCallError

from karen.tools.local import file_create_directory_tool


async def test_directory_tool_creates_missing_parents_and_rejects_escape(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    tool = file_create_directory_tool(root)
    context = SimpleNamespace(cancellation_token=CancellationToken(), deadline=time.monotonic() + 5)

    for _ in range(2):
        result = await tool.handler({"path": "project/app"}, context)
        assert result == {"path": str(root / "project/app")}
        assert (root / "project/app").is_dir()

    with pytest.raises(ToolCallError):
        await tool.handler({"path": "escape/project"}, context)
    assert not (outside / "project").exists()

    (root / "file").write_text("existing file")
    with pytest.raises(ToolCallError):
        await tool.handler({"path": "file/nested"}, context)
    assert (root / "file").read_text() == "existing file"
