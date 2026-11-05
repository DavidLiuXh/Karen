# Karen

English | [简体中文](README_cn.md)

A personal agent that remembers useful context and turns your requests into results.

Tell Karen what you need. It retrieves relevant memories, asks for missing information when necessary, and uses tools to research, organize, and deliver the work. Use it in your terminal or through personal WeChat.

## What you can do

- **Keep context:** remember your preferences and recall past conversations.
- **Get work done:** research topics and save findings as text or HTML.
- **Manage tasks:** run independent tasks concurrently and schedule recurring requests.
- **Reuse successful work:** compatible requests can use verified workflows.
- **See what happened:** inspect decisions, tool calls, and results in a local dashboard.

```text
I like history, archaeology, and cycling.
What are my hobbies?
Research today's GitHub Trending projects and save a summary to /tmp/trending.html.
/schedule Every day at noon, summarize the latest technology news.
```

Karen is under active development. Task results depend on available tools and model decisions; proactive task discovery is not yet supported.

## Quick start

You need Python 3.11 or 3.12, [uv](https://docs.astral.sh/uv/getting-started/installation/), [Ollama](https://ollama.com/download), and a DeepSeek API key.

**1. Install from source.** Keep the two repositories beside each other:

```bash
git clone https://github.com/DavidLiuXh/DynamicAgentGraph.git
git clone https://github.com/DavidLiuXh/Karen.git
cd Karen
uv sync --python 3.12
```

**2. Prepare memory embeddings.** With Ollama running, download the model:

```bash
ollama pull bge-m3:latest
```

**3. Configure API keys.** Create `.env` in the Karen directory:

```dotenv
DEEPSEEK_API_KEY=your_deepseek_api_key
TAVILY_API_KEY=your_tavily_api_key
```

Tavily is optional and enables web search. `.env` is ignored by Git.

**4. Start Karen:**

```bash
uv run --env-file .env karen --observe
```

Open the printed dashboard address, usually `http://127.0.0.1:8765/`. Omit `--observe` if you only want the terminal. Use `--timezone Asia/Shanghai` to set your timezone explicitly.

## Everyday use

| Command | Purpose |
| --- | --- |
| `/new request` | Start an independent task |
| `/tasks` | List tasks and their IDs |
| `/reply ID answer` | Answer a task's clarification question |
| `/cancel ID` | Cancel a task |
| `/schedule request` | Create or change a schedule in natural language |
| `/schedule list` | List schedules |
| `/exit` | Exit Karen |

Schedules persist across restarts, but **trigger only while Karen is running**. Missed occurrences are not replayed.

To use personal WeChat, run the following and scan the QR code:

```bash
uv run --env-file .env karen --wechat
```

See [WeChat setup](docs/WEIXIN.md) for details. Gmail read-only access is also available with [separate OAuth configuration](docs/GMAIL.md); continuous new-mail monitoring is not implemented yet.

## See the work

![Karen dashboard showing an execution graph and task timeline](docs/images/observability-execution.jpg)

Memory and runtime records are stored under `~/.Karne/`. Required inputs are sent to the configured model and search services, which may incur API charges.

## Learn more

- [Detailed guide](docs/GUIDE.md): architecture, configuration, workflows, memory, and development.
- [Task management](docs/TASK_MANAGER.md) · [Observability](docs/OBSERVABILITY.md)
- [Evaluation reports](docs/GUIDE.md#evaluation-results): historical results, scope, and known limitations.

Task planning and execution are powered by [DynamicAgentGraph](https://github.com/DavidLiuXh/DynamicAgentGraph).
