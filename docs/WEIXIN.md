# Personal Weixin channel

[中文说明](WEIXIN_cn.md)

Karen connects to ordinary personal Weixin through Tencent iLink. It speaks the HTTP
protocol directly; no OpenClaw installation, enterprise WeCom account, public callback
server, client injection, or desktop UI automation is required. Account availability
and login acceptance are determined by Weixin. The protocol reference is
[Tencent/openclaw-weixin 2.4.9](https://github.com/Tencent/openclaw-weixin/blob/24de5c9eb0dd5e595d7e2d090ed8a3f82870d42c/docs/protocol_zh_CN.md).
The current channel uses POSIX file locks and supports macOS/Linux.

## Start

Install Karen and configure its model/embedding backends as described in the README.
For QR binding alone, model keys are unnecessary:

```bash
uv run karen --wechat-login
```

Scan the terminal QR code using your own phone's Weixin and confirm on the phone.
If Weixin requests a verification code, enter it at the terminal. Login is bounded to
five minutes, with up to three QR refreshes. The owner is taken from the confirmed
server response, never from the first incoming message.

```bash
uv run --env-file .env karen --wechat --timezone Asia/Shanghai --observe
```

`--wechat` uses Weixin instead of the terminal conversation. It automatically offers
QR binding when credentials are absent. The original terminal mode remains:

```bash
uv run --env-file .env karen --observe
```

Only private messages from the binding owner enter Karen. Other senders, groups,
bot messages and incomplete messages are ignored. Do not run multiple Karen processes
against the same memory/channel directory. Stop with Ctrl+C; normal shutdown drains
the existing asynchronous memory writer. Stop the running channel before rebinding.
If credentials expire, the channel stops with `WEIXIN_SESSION_EXPIRED`; run
`--wechat-login` and restart. Rebinding to a different human owner is rejected to
prevent sharing one person's global memory with another person.

The Weixin session retains its pending clarification across restarts. It uses the
same intent recognition, DynamicAgentGraph execution, global context memory and
observation pipeline as the terminal. There is no extra LLM routing layer. The
effective timezone is the startup `--timezone` (or the local machine's timezone).
Pending clarification retains its original UTC time anchor. This version has one owner and one ordered
private conversation, with independent receive, execution and delivery loops.
Relative dates are anchored to the inbound message's server timestamp (local receipt
time when absent), so time spent queued across midnight does not change “tomorrow”.

## Files and long tasks

Send an attachment using Weixin's **file** message type, followed by your request,
or attach the request and file in one message. Files are bounded to **10 MiB each**,
decrypted and saved into a private random directory under `/tmp`. File names cannot
select arbitrary local paths. Metadata and the local path enter the normal Karen
input pipeline. The existing `file.read_text` tool reads UTF-8 text up to 1 MiB;
this channel does not add PDF/image/audio understanding. Unsupported message types
receive an explicit error rather than being silently discarded.

To receive a generated file, ask explicitly, for example:

> Produce an HTML report under /tmp/report.html and send the file back in Weixin.

The engine can call `weixin.prepare_file@1.0.0` after the file producer. This tool
validates a file under `/tmp` and saves an immutable private snapshot. Following
successful task execution, the channel uploads that snapshot and sends a file
message to the owner. An arbitrary path appearing in model output is never treated
as authorization to send a file. Preparation is not delivery confirmation.

Responses are split into Unicode-safe text messages of at most 4 KB. After 15 seconds
of execution, a progress acknowledgement is queued while the task continues.
Typing signals are best effort. A slow task does not stop receiving subsequent
messages or delivering previous results; subsequent tasks run in arrival order.

## Durability and recovery

| Situation | Behavior |
| --- | --- |
| Duplicate message / reconnect replay | Unique message ID prevents re-execution |
| Received batch | Inbox and opaque polling cursor commit in one SQLite transaction |
| Pending task at restart | Processed in order |
| Completed task, unsent result at restart | Deliver the persisted result without rerunning the task |
| Process dies while executing | Mark the outcome uncertain and notify the owner; never automatically redo potentially completed side effects |
| Network / HTTP 429 / HTTP 5xx during send | Retry the outbox entry with its original client ID, up to five attempts with bounded backoff |
| Permanent send rejection / exhausted retries | Retain the failed entry; `/retry` requeues delivery using the new inbound context token |
| Missing or expired credentials | Stop with a visible diagnostic; rebind explicitly |

Weixin commands:

- `/status`: queued tasks and pending/failed delivery counts.
- `/retry`: retry failed message delivery, **without rerunning any task**.

Polling resumes from the persisted cursor after transient failures. Delivery keeps
the original inbound `context_token`, and the uploaded file reference is persisted
before sending, so send retries do not reupload it. Chunks and files from a response
preserve order; a permanently failed chunk blocks the rest of that response.

Exactly-once delivery is not promised: the reference does not document server-side
deduplication for repeated `client_id`. A lost acknowledgement can produce a duplicate
reply, but must never cause repeated task execution. Likewise, recovery cannot prove
whether a side effect finished just before a crash. Delayed replies rely on Weixin
accepting their context token; no unverified time window or unrestricted proactive
push capability is assumed. Execution-time approval actions and unsolicited proactive
notifications are outside this channel's current scope.
A successful send means the server acknowledged `ret=0`, not a read receipt from the owner.

## Local data and observation

- `~/.Karne/weixin/credentials.json`: private bearer credentials (0600).
- `~/.Karne/weixin/<account hash>/state.sqlite3`: inbox, cursor, conversation checkpoint,
  delivery status and file references (0600).
- `~/.Karne/weixin/<account hash>/artifacts/`: prepared file snapshots (0700 directories,
  0600 files), retained for delivery recovery.
- `/tmp/karen-weixin-*`: received files (0700 directories, 0600 files).
- Existing memory and observation directories are shared with terminal mode.

These stores contain personal conversation data. They are local and access-restricted,
not encrypted at rest. Files and transport history are retained; no automatic deletion
policy is introduced. `/tmp` attachments can disappear after OS cleanup. Credentials,
context tokens, typing tickets, media keys and CDN parameters are excluded/redacted
from observation and memory; raw transport payloads remain only in the private inbox.
HTTPS requests do not follow redirects and permit only Weixin domain endpoints.

With `--observe`, the dashboard exposes **最近微信渠道事件** for receive/processing/send
events and safe error codes. Agent tasks still appear in the normal interaction list;
delivery events link to their task trace. Model reasoning and memory diagnostics remain
in this local dashboard instead of being appended to ordinary replies.

## Verification

Automated tests use mock HTTP responses and the real intent/engine path to exercise
QR verification, owner isolation, encryption and bounds, deduplication, atomic cursor
updates, multi-round clarification across restart, task/memory/trace integration,
transport-only retries, persisted file upload references, interruption recovery and
CLI lifecycle. They do not establish acceptance by a live Weixin account.

After binding a real account, verify:

1. Send a short message and confirm a reply.
2. Start an underspecified task, restart Karen after its question, then answer it;
   confirm the original task continues.
3. Run a task taking more than 15 seconds; confirm progress and the final reply.
4. Send a UTF-8 file and request a summary; request an HTML file back.
5. Interrupt connectivity, then restore it; confirm queued results arrive without
   repeated task execution, and inspect channel events.
6. Stop with Ctrl+C and restart; confirm memory is available and the terminal works.

Live QR scanning, message acceptance and real CDN delivery require the owner's phone
and must be verified separately from automated tests.
