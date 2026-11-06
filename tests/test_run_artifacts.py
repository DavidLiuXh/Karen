import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from dynamic_graph import CancellationToken
from dynamic_graph.execution.errors import ToolCallError

from karen.tools.run_artifacts import run_artifacts_tool


def context():
    return SimpleNamespace(cancellation_token=CancellationToken(), deadline=time.monotonic() + 10)


@pytest.fixture
def recorded(tmp_path):
    run = tmp_path / ('a' * 32)
    artifacts = run / 'artifacts'
    artifacts.mkdir(parents=True)
    (run / 'manifest.json').write_text(json.dumps({'terminal': True}))
    (run / 'events.jsonl').write_text(json.dumps({
        'event_type': 'node_returned', 'payload_ref': 'artifacts/extract-1.json'}) + '\n')
    (artifacts / 'extract-1.json').write_text('{"answer":"实际产物"}')
    (artifacts / 'unrecorded.json').write_text('unrecorded')
    return tmp_path, run, artifacts


async def test_reads_only_recorded_outputs_with_names_and_content(recorded):
    root, _, artifacts = recorded
    tool = run_artifacts_tool(root)
    result = await tool.handler({'path': str(artifacts)}, context())
    assert result == {'complete': True, 'files': [
        {'name': 'extract-1.json', 'path': str(artifacts / 'extract-1.json'),
         'content': '{"answer":"实际产物"}'}]}


@pytest.mark.parametrize('kind', ['outside', 'unfinished', 'symlink', 'oversized', 'metadata'])
async def test_rejects_outside_unfinished_symlink_and_oversized_reads(recorded, kind):
    root, run, artifacts = recorded
    path = artifacts
    if kind == 'outside':
        path = root.parent / 'outside' / run.name / 'artifacts'
    elif kind == 'unfinished':
        (run / 'manifest.json').write_text('{"terminal":false}')
    elif kind == 'symlink':
        (artifacts / 'extract-1.json').unlink()
        (artifacts / 'extract-1.json').symlink_to(run / 'manifest.json')
    elif kind == 'oversized':
        (artifacts / 'extract-1.json').write_bytes(b'x' * 1_048_577)
    elif kind == 'metadata':
        path = run
    with pytest.raises(ToolCallError):
        await run_artifacts_tool(root).handler({'path': str(path)}, context())


async def test_cancellation_and_expired_deadline(recorded):
    root, _, artifacts = recorded
    ctx = context()
    ctx.cancellation_token.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_artifacts_tool(root).handler({'path': str(artifacts)}, ctx)
    ctx = context()
    ctx.deadline = time.monotonic() - 1
    with pytest.raises(TimeoutError):
        await run_artifacts_tool(root).handler({'path': str(artifacts)}, ctx)
