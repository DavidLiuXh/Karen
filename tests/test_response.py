"""User-visible diagnostic boundaries shared by CLI and WeChat."""

import pytest
from dynamic_graph import RunResult

from karen.response import format_result


@pytest.mark.parametrize("status,complete", [
    ("COMPLETED", True), ("COMPLETED", False), ("FAILED", False), ("CANCELLED", False),
])
@pytest.mark.parametrize("warning_code", ["NO_DATA_DEPENDENCY", "INTERNAL_GRAPH_ADVICE"])
def test_internal_warnings_are_not_appended_to_answers(status, complete, warning_code):
    result = RunResult(
        run_id="diagnostics", execution_status=status, output_complete=complete,
        outputs={"answer": "已生成 HTML。"},
        diagnostics=[
            {"code": warning_code, "phase": "validation", "severity": "warning",
             "message": "Internal implementation advice"},
            {"code": "TOOL_FAILED", "phase": "execution", "message": "文件写入失败。"},
        ],
    )
    original = result.model_dump(mode="json")
    text = format_result(result)
    assert warning_code not in text and "Internal implementation advice" not in text
    assert ("文件写入失败。" in text) is (status != "COMPLETED" or not complete)
    if status == "COMPLETED" and complete:
        assert text == result.outputs["answer"]
    assert result.model_dump(mode="json") == original
    assert original["diagnostics"][0]["code"] == warning_code


@pytest.mark.parametrize("status,complete", [("COMPLETED", True), ("FAILED", False)])
def test_actionable_diagnostics_remain_visible(status, complete):
    result = RunResult(
        run_id="action-needed", execution_status=status, output_complete=complete,
        outputs={"answer": "已有部分结果。"},
        diagnostics=[{
            "code": "AUTHORIZATION_NEEDED", "phase": "execution", "severity": "warning",
            "action": "REVIEW_POLICY", "message": "请检查文件写入权限。",
        }],
    )
    assert "请检查文件写入权限。" in format_result(result)
