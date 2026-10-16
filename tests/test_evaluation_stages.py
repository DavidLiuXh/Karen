import pytest

from karen.evaluation.stages import evidence_presence, ranking_quality, stage_metrics


def test_evidence_checks_original_sources_and_accepts_annotated_alternatives():
    expectation = {"required_quote_groups": [["Moved to Porto", "settled in Porto"], ["on Monday"]]}
    records = [
        {
            "memory": {
                "text": "Translated summary",
                "sources": [{"quote": "I settled in Porto on Monday."}],
            }
        }
    ]
    assert evidence_presence(expectation, records)["status"] == "present"
    assert (
        evidence_presence(expectation, [{"memory": {"text": "Moved to Porto on Monday"}}])["status"]
        == "missing"
    )
    assert evidence_presence(expectation, None)["status"] == "unobserved"
    assert evidence_presence(None, records)["status"] == "unscored"


def test_correct_action_and_evidence_do_not_mask_execution_failure():
    case = {
        "steps": [
            {
                "text": "use the saved address",
                "expected": {"outcome": "complete"},
                "evidence_expectation": {"required_quote_groups": [["Porto"]]},
            }
        ]
    }
    result = {
        "status": "failed",
        "steps": [
            {
                "outcome": "execution",
                "execution_status": "FAILED",
                "output_complete": False,
                "memory": {
                    "status": "degraded",
                    "degradations": ["RERANK_FAILED_FUSION_ORDER"],
                    "m1": [{"memory": {"sources": [{"quote": "Porto"}]}}],
                },
            }
        ],
    }
    metric = stage_metrics(case, result)["steps"][0]
    assert metric["public_action"]["matched"]
    assert metric["packed_evidence"]["status"] == "present"
    assert metric["execution"]["status"] == "FAILED"
    assert metric["recall_status"] == "degraded"


def test_missing_turn_is_not_a_correct_nonquestion_or_successful_retrieval():
    metric = stage_metrics(
        {
            "steps": [
                {
                    "expected": {"outcome": "complete"},
                    "evidence_expectation": {"required_quote_groups": [["fact"]]},
                }
            ]
        },
        {"status": "error", "error_code": "MODEL_TIMEOUT", "stage": "interaction"},
    )
    assert metric["runtime_error"] == "MODEL_TIMEOUT"
    assert not metric["steps"][0]["public_action"]["matched"]
    assert metric["steps"][0]["packed_evidence"]["status"] == "unobserved"


def test_valid_ranking_can_still_lose_required_evidence_or_include_distractors():
    ranking = [
        {"memory_id": "a", "relevance": "irrelevant"},
        {"memory_id": "b", "relevance": "relevant"},
    ]
    result = ranking_quality(["a", "b"], ranking, [["a"]], ["b"])
    assert result["status"] == "fail" and result["missing_groups"] == [0]
    assert result["irrelevant_retained"] == ["b"]
    assert ranking_quality(["a"], None, [["a"]], [])["status"] == "error"
    assert ranking_quality(["a"], ranking, [["a"]], [])["status"] == "invalid_ids"
    with pytest.raises(ValueError):
        ranking_quality(["a"], ranking, [["not-a-candidate"]], [])
