import time
from types import SimpleNamespace

import pytest
from dynamic_graph import CancellationToken
from dynamic_graph.execution.errors import ToolCallError

from karen.tools.git import git_clone_tool


def clone_context():
    return SimpleNamespace(cancellation_token=CancellationToken(), deadline=time.monotonic() + 5)


@pytest.mark.parametrize(
    "url,path,code",
    [
        ("file:///tmp/repo", "checkout", "GIT_URL_INVALID"),
        ("https://token@github.com/org/repo", "checkout", "GIT_URL_INVALID"),
        ("https://github.com/org/repo", "../outside", "GIT_PATH_INVALID"),
        ("https://github.com/org/repo", "missing/checkout", "GIT_PARENT_MISSING"),
        ("https://github.com/org/repo", "existing", "GIT_DESTINATION_EXISTS"),
    ],
)
async def test_clone_rejects_unsafe_or_existing_destinations(tmp_path, url, path, code):
    (tmp_path / "existing").write_text("keep")
    with pytest.raises(ToolCallError) as error:
        await git_clone_tool(tmp_path).handler({"url": url, "path": path}, clone_context())
    assert error.value.code == code
    assert (tmp_path / "existing").read_text() == "keep"


async def test_failed_clone_cleans_up_without_publishing_destination(tmp_path, monkeypatch):
    executable = tmp_path / "git"
    executable.write_text('#!/bin/sh\nfor arg do dest="$arg"; done\nmkdir "$dest"\nexit 17\n')
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + ":/bin")
    with pytest.raises(ToolCallError) as error:
        await git_clone_tool(tmp_path).handler(
            {"url": "https://github.com/org/repo", "path": "checkout"}, clone_context()
        )
    assert error.value.code == "GIT_CLONE_FAILED"
    assert not (tmp_path / "checkout").exists()
    assert not list(tmp_path.glob(".karen-git-*"))
