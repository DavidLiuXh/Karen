# Clarification evaluation protocol 2

Protocol 1 remains available and unchanged, including its frozen judge transport.
Protocol 2 is opt-in through `protocol_version: "2"` in a newly frozen manifest.
Use `python -m karen.evaluation prepare-v2 --manifest OLD --output NEW` to create
one exclusively; it preserves public labels, references and records the original hash.
The usual `run` command accepts either version. Original runs are never overwritten.

## Independent dimensions

- **Public action agreement:** final ask/no-ask against the original label. Report
  TP/FP/FN/TN, action accuracy, clarification recall and overclarification rate.
  An exception/missing response is an error in the original denominator, not a correct non-ask.
- **Product action agreement:** the same metrics against an optional, separately
  authored per-step `policy_expectation`. Unannotated cases remain unscored, not passed.
  No product expectation is inferred from a response or substituted for a public label.
- **Quality:** when the action agrees, grade `question_quality`, `answer_quality` or
  `goal_quality` separately. The judge receives the explicit expectation, full current
  turn chain, provided context and relevant reference. It may return pass/fail/disputed.
  A question need not provide the final answer. A goal does not prove task completion.
- **Repetition:** the quality judge flags asking again for sufficiently answered
  information. A legitimately pending question is not a repetition error. Report the
  number of questions actually graded alongside the rate; missing grades are not zero errors.
- **Multi-turn completion:** report complete passed non-CLAMBER conversations with a
  final reply/execution against all selected multi-turn cases. Intent-only goals are excluded.

Runtime errors, judge errors and disputed references remain visible. Public quality
judgments cannot overwrite hard action/contract checks. The aggregate result retains
the public-label interpretation; product-policy dimensions are explicitly separate.
Each dimension also reports `case_status_counts`: a correct action cannot hide a
failed quality grade or judge error. Unannotated product cases are `unscored`.
These counts cover that dimension's annotated checks; missing quality references
remain ungraded. `confusion` counts each turn once; `error_breakdown` explains the
error category without adding duplicate entries to the confusion matrix.

Example independent annotation, frozen before running:

```json
"policy_expectation": {
  "outcome": "clarification",
  "necessary_information": ["Unique operation target"],
  "reference": "Ask which object should be modified.",
  "rationale": "The action cannot use a default target."
}
```

Gold/reference/expectation data enters only evaluation, never Karen's model inputs.
Use the same schema on each step of a multi-turn conversation, not only its final turn.

## Judge calibration

```bash
python -m karen.evaluation calibrate --output runs/evaluation/judge-calibration-v2
```

The curated fixture covers Chinese/English questions, private targets, distracting
questions, partial answers, repeated answered questions, unknown memory, honest failed
verification and incorrect final answers. Its expected verdicts and repetition flags
are fixed before calling the judge. Agreement requires both to match; all cases must
match for `calibrated_on_curated_set=true`. Errors count as mismatches.

These references are agent-authored and manually checked against product policy, **not
independently human validated**. The output retains that provenance, the exact fixture,
its hash, each grade and model profile/usage. Human review remains pending and training
reward approval remains false even at 100% agreement. This is a useful initial
calibration check, not proof that the judge is reliable in every domain.

## Regrade and trace diagnosis

```bash
python -m karen.evaluation regrade --source OLD_RUN --suite clamber --output NEW_RUN
```

This never invokes Karen or reruns tools. It creates independent protocol-2 grades of
saved responses, preserves the source result/manifest hashes, and reports
`mode=regrade_saved_responses`. It is not latest-code model performance. Original
failures and grades remain untouched. Trace origin records identify questions from
clarity, direct response, assessment/review roles and the common policy exit when present.
Large original artifacts remain local; documentation records the reproducible commands
and their limitations. The 24 seen CLAMBER records remain regression data, not training
examples or evidence of generalization.

## Stage diagnostics (2026-10-12)

New results also contain `stage_metrics`, without changing existing scores or labels:
public/product action agreement, packed evidence presence, recall degradation, execution
status and runtime error are independent fields. This is recorded for both protocols;
it does not retroactively modify historical artifacts or certify task completion.

Optional per-step annotations must be frozen before a run:

```json
"evidence_expectation": {
  "required_quote_groups": [["original source quote", "alternative equivalent quote"]]
}
```

Every group requires at least one matching original source quote (or sourced m3 text).
Summary text alone does not count. This measures presence, not factual correctness or
successful use of the evidence. Unannotated cases are `unscored`; missing observations
are `unobserved`; malformed groups are `annotation_error`. This metric covers the
packed context, not extraction or coarse retrieval: diagnosing those stages still
requires their saved trace/storage artifacts. Aggregate benchmark scores are unchanged.

Fixed-input rerank experiments separately score required candidate groups, retained
irrelevant candidates, candidate-ID validity and historical task selection. JSON validity
alone is not a semantic pass. `tests/fixtures/rerank_acceptance_v1.json` is a small
agent-authored synthetic fixture, not an independently validated benchmark.
