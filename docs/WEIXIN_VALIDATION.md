# Weixin channel validation — 2026-10-08

Implementation reference: Tencent/openclaw-weixin **2.4.9**, commit
`24de5c9eb0dd5e595d7e2d090ed8a3f82870d42c`.

## Automated checks

```bash
uv run pytest -q
uv run pytest -q tests/test_weixin_protocol.py tests/test_weixin_channel.py tests/test_weixin_cli.py tests/test_observability.py
uv run ruff check .
git diff --check
```

Final full regression passed **454 tests** in 272.98 seconds. Ruff and `git diff --check`
also passed. The channel/observation batch passed **76 tests**. It includes real Karen intent and
DynamicAgentGraph execution with deterministic model responses, actual local file
production and artifact preparation, mocked iLink/CDN HTTP traffic, private SQLite
storage and restart tests. No real owner conversations or global memory were used.

Covered behaviors:

- QR verification, authenticated headers, URL restrictions and bounded response bodies.
- Owner-only messages, deduplication, transactional inbox/cursor, process locking.
- Clarification continuing after restart; message-time date anchors preserved in memory.
- Poll reconnect, expired credentials, transport-only retries with stable client IDs.
- Completed results surviving restart; interrupted execution is never replayed automatically.
- Unicode response chunking and ordering, failed-delivery retry commands.
- AES file transfer, traversal/size limits, file snapshots and cached upload references.
- Actual response capture, including text delivered before an attached file completes.
- Normal CLI defaults, model-independent binding, lifecycle and safe storage errors.
- Channel diagnostics, secret redaction, and concurrent observation reads.

During full regression, the existing PTY subprocess tests intermittently exceeded their
10/30-second startup deadlines before emitting any output. Their independent rerun
passed; timeouts and assertions were not weakened. A separate observation read race
was reproduced and fixed: a bare input span is no longer listed as a blank interaction
before its input event is written, while failed/corrupt records remain inspectable.

## Manual and live checks

The observation dashboard was opened in a real browser using synthetic records.
Channel events could be expanded, and a failed send showed its safe error code and
attempt count in a separate channel trace, without an empty task identity.

The live iLink QR endpoint responded successfully and valid QR images were generated.
No phone confirmation arrived during the five-minute login period; login timed out
as designed. **No live account was bound, and real text/file delivery is not yet verified.**

The owner must run `uv run karen --wechat-login`, scan and confirm, then start:

```bash
uv run --env-file .env karen --wechat --timezone Asia/Shanghai --observe
```

Complete the live-account checklist in [English](WEIXIN.md#verification) or
[中文](WEIXIN_cn.md#验证). Automated results must not be read as proof of live message
acceptance, read receipts, context-token lifetime or unrestricted proactive delivery.

## Live text-delivery incident and fix — 2026-10-08

After the initial validation, the owner bound an account and received a progress
notification but no final answer. The task completed in about 30 seconds. Karen
incorrectly required an explicit `ret=0` from `sendmessage`; Tencent's pinned
implementation accepts a valid JSON object with `ret` omitted. The notification
was therefore marked failed, and response ordering blocked the saved answer behind it.

Sending now follows that response contract while still rejecting invalid JSON,
HTTP errors and nonzero API error codes. Progress notifications have an explicit
outbox flag, are attempted once, and cannot block final text, files or response
memory capture. Completion supersedes unsent progress. Startup transactionally
migrates the original notification format and preserves response IDs and failed
notification diagnostics; required response chunks still retain strict ordering.

The focused channel/observation suite passed **90 tests** in 6.23 seconds, including
optional `ret`, malformed/rejected responses, failed progress, completion during
an in-flight notification, and recovery of the incident's original SQLite format
across repeated restarts without agent execution. Ruff and `git diff --check` passed.

The owner's idle service was gracefully restarted. Its previously pending final
text was sent with its original client ID; the outbox is now `sent`, a `weixin.sent`
event is recorded, and the actual response was captured in memory. The earlier
notification remains auditable as `superseded` with its original error and attempts.
This verifies live server acceptance of that final text, not a read receipt. Live
file delivery and the remaining manual checklist are still unverified.
