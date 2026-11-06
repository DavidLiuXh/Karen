"""Minimal terminal conversation using the existing model transport."""

import argparse
import asyncio
import os
import re
import sqlite3
import sys
import threading
from contextlib import AsyncExitStack, nullcontext
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

from dynamic_graph import DynamicGraphEngine, EngineConfig, ModelBindings
from dynamic_graph.tools import (
    browser_open_local_page_tool,
    file_read_text_tool,
    file_write_text_tool,
    tavily_search_tool,
    web_fetch_tool,
)
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import InMemoryHistory
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from .agent import Karen
from .context import ContextMemory
from .context.contracts import PersistenceError
from .intent import IntentRecognizer, IntentSession
from .message import messages
from .model.models import deepseek_client, memory_embeddings
from .observability import ObservedModel, Observer
from .observability.viewer import DEFAULT_PORT, background_server
from .schedule import ScheduledTasks
from .task import DEFAULT_MAX_CONCURRENT_TASKS, TaskManager
from .task.console import run_console
from .task.resources import ExecutionResources
from .tools.gmail import GmailClient, GmailCredentials
from .tools.gmail_tools import gmail_tools
from .tools.run_artifacts import run_artifacts_tool
from .tools.web import web_read_page_tool
from .workflow import ReusableWorkflows

# Keep one history for the lifetime of the CLI process so each prompt can
# browse commands entered at earlier prompts.
_input_history = InMemoryHistory()


def display_response(text: str, *, json_output: bool = False) -> None:
    """Render terminal replies while keeping redirected and JSON output unchanged."""
    if json_output:
        print(text)
    elif sys.stdout.isatty() and os.environ.get("TERM") != "dumb":
        console = Console(file=sys.stdout)
        console.print()
        if text == messages.TASK_HELP:
            console.print(Text(messages.CLI_TITLE, style="bold magenta"))
            console.print(Text(text, style="dim"))
        else:
            console.print(Text(messages.KAREN_PREFIX, style="bold magenta"))
            # Only decorate the event envelope; leave reply contents as Markdown.
            header, separator, body = text.partition("\n")
            match = re.fullmatch(r"\[[^\]\n]+ · ([^\]\n]+)\]", header)
            state_styles = {
                messages.TASK_STATE_LABELS["queued"]: "cyan",
                messages.TASK_STATE_LABELS["running"]: "cyan",
                messages.TASK_STATE_LABELS["waiting"]: "yellow",
                messages.TASK_STATE_LABELS["completed"]: "green",
                messages.TASK_STATE_LABELS["failed"]: "red",
                messages.TASK_STATE_LABELS["cancelled"]: "dim",
            }
            if separator and match and match[1] in state_styles:
                console.print(Text(header, style=state_styles[match[1]]))
                console.print(Markdown(body))
            else:
                console.print(Markdown(text))
        console.print()
    else:
        print(messages.KAREN_PREFIX + text)


def create_memory(model, *, rerank_model=None, observer=None) -> ContextMemory:
    return ContextMemory(
        root_dir=Path.home() / ".Karne" / "context",
        model=model,
        rerank_model=rerank_model,
        embeddings=memory_embeddings(),
        observer=observer,
    )


def create_workflows(model, engine, observer):
    return ReusableWorkflows(
        root_dir=Path.home() / ".Karne" / "workflows",
        runs_dir=engine.config.runs_dir, model=model, observer=observer,
    )


def create_observer() -> Observer:
    return Observer(
        Path.home() / ".Karne" / "observability",
        sensitive_values=(os.environ.get("DEEPSEEK_API_KEY"), os.environ.get("TAVILY_API_KEY")),
    )


def create_schedules(manager, session, model):
    return ScheduledTasks(
        root_dir=Path.home() / ".Karne" / "schedules" / "cli",
        manager=manager, session=session, model=model,
    )


async def read_input() -> str:
    """Cancel terminal editing cleanly; keep redirected input compatible with input()."""
    if sys.stdin.isatty():
        # Async editing restores terminal modes before cancellation or Ctrl+C propagates.
        prompt = messages.INPUT_PROMPT
        if os.environ.get("TERM") != "dumb":
            style = "" if "NO_COLOR" in os.environ else "bold ansicyan"
            prompt = FormattedText([(style, messages.CLI_INPUT_PROMPT)])
        return await PromptSession(history=_input_history).prompt_async(prompt)

    # A cancelled pipe read must not hold asyncio's executor shutdown open.
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def complete(value, error):
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(value)

    def read():
        value, error = None, None
        try:
            value = input(messages.INPUT_PROMPT)
        except BaseException as exc:
            error = exc
        try:
            loop.call_soon_threadsafe(complete, value, error)
        except RuntimeError:
            pass  # The process may finish while its daemon console reader is blocked.

    threading.Thread(target=read, name="karen-console-input", daemon=True).start()
    return await future


async def register_gmail_tools(engine, resources, resources_guard):
    """Enable Gmail only when the complete existing OAuth grant is configured."""
    names = ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")
    values = [os.environ.get(name, "").strip() for name in names]
    if not any(values):
        return
    if not all(values):
        print(messages.GMAIL_CONFIG_INCOMPLETE, file=sys.stderr)
        return
    import httpx

    http = await resources.enter_async_context(httpx.AsyncClient())
    client = GmailClient(http, GmailCredentials(*values))
    for definition in gmail_tools(client):
        engine.register_tool(resources_guard.independent_read(definition))


async def converse(
    *, json_output: bool = False, timezone: str | None = None, observe_port: int | None = None,
    wechat: bool = False, max_concurrent_tasks: int = DEFAULT_MAX_CONCURRENT_TASKS,
) -> int:
    try:
        session = IntentSession(timezone=timezone) if timezone is not None else IntentSession()
    except (ValueError, ZoneInfoNotFoundError, OSError):
        print(messages.INVALID_TIMEZONE)
        return 1
    credentials = await bind_weixin() if wechat else None
    backend = deepseek_client()
    observer = create_observer()
    async with AsyncExitStack() as resources:
        await observer.start()
        resources.push_async_callback(observer.close)
        if observe_port is not None:
            if observer.root_dir is None:
                print(messages.OBSERVATION_STORAGE_UNAVAILABLE, file=sys.stderr)
                return 1
            try:
                server = resources.enter_context(
                    background_server(observer.root_dir, port=observe_port)
                )
            except OSError:
                print(
                    messages.OBSERVATION_PORT_UNAVAILABLE.format(port=observe_port),
                    file=sys.stderr,
                )
                return 1
            print(
                messages.OBSERVATION_STARTED.format(port=server.server_port),
                file=sys.stderr, flush=True,
            )
        model = ObservedModel(backend, observer)
        engine = DynamicGraphEngine(
            config=EngineConfig(
                runs_dir=Path.home() / ".Karne" / "runs" if observer.root_dir else Path("runs"),
                sensitive_values=observer.sensitive_values,
            ),
            models=ModelBindings(
                planner=ObservedModel(deepseek_client(reasoning_effort="medium"), observer),
                worker=model,
            ),
        )
        resources_guard = ExecutionResources(observer)
        for tool_factory in (
            file_read_text_tool,
            file_write_text_tool,
            browser_open_local_page_tool,
            web_fetch_tool,
            web_read_page_tool,
        ):
            definition = tool_factory()
            if tool_factory not in {web_fetch_tool, web_read_page_tool}:
                definition = resources_guard.file_tool(
                    definition, write=tool_factory is file_write_text_tool,
                )
            else:
                definition = resources_guard.independent_read(definition)
            engine.register_tool(definition)
        Path(engine.config.runs_dir).mkdir(parents=True, exist_ok=True)
        engine.register_tool(resources_guard.file_tool(
            run_artifacts_tool(engine.config.runs_dir), root=engine.config.runs_dir,
        ))
        if os.environ.get("TAVILY_API_KEY", "").strip():
            engine.register_tool(resources_guard.independent_read(tavily_search_tool()))
        else:
            print(messages.TAVILY_UNAVAILABLE)
        await register_gmail_tools(engine, resources, resources_guard)
        memory_model = ObservedModel(deepseek_client(thinking=False), observer)
        memory = create_memory(memory_model, rerank_model=model, observer=observer)
        try:
            await memory.start()
        except (OSError, PersistenceError, RuntimeError):
            print(messages.MEMORY_STORAGE_UNAVAILABLE)
            return 1
        workflows = create_workflows(memory_model, engine, observer)
        try:
            if workflows is not None:
                try:
                    await workflows.start()
                except (OSError, ValueError, sqlite3.Error):
                    observer.emit("workflow.unavailable", status="failed", data={"reason": "storage_unavailable"})
                    workflows = None
            agent = Karen(
                intent=IntentRecognizer(model, observer=observer),
                engine=engine,
                memory=memory,
                observer=observer,
                workflows=workflows,
                resources=resources_guard,
            )
            if wechat:
                import httpx

                from .channels.weixin import (
                    ILinkClient,
                    WeixinChannel,
                    WeixinStore,
                    prepare_file_tool,
                )

                store = WeixinStore(Path.home() / ".Karne" / "weixin", credentials)
                resources.callback(store.close)
                engine.register_tool(resources_guard.file_tool(prepare_file_tool(store)))
                http = await resources.enter_async_context(httpx.AsyncClient(trust_env=False))
                channel = WeixinChannel(
                    agent=agent, client=ILinkClient(http, token=credentials.token,
                                                  base_url=credentials.base_url),
                    store=store, credentials=credentials, initial_session=session, observer=observer,
                    max_concurrent_tasks=max_concurrent_tasks,
                    schedule_model=memory_model,
                )
                print(messages.WEIXIN_STARTED, file=sys.stderr)
                await channel.run()
                return 0
            from prompt_toolkit.patch_stdout import patch_stdout

            manager = TaskManager(agent, max_concurrent=max_concurrent_tasks, observer=observer)
            schedules = create_schedules(manager, session, memory_model)
            if schedules is not None:
                resources.callback(schedules.close)
            with patch_stdout(raw=True) if sys.stdout.isatty() else nullcontext():
                return await run_console(manager, session, read_input=read_input,
                                         display=display_response, json_output=json_output,
                                         schedules=schedules)
        finally:
            try:
                try:
                    if workflows is not None:
                        await workflows.close()
                finally:
                    await memory.close()
            except PersistenceError:
                print(messages.EXIT_PERSISTENCE_FAILED)
                raise


async def bind_weixin(*, force=False):
    """Binding does not require a model key and never prints a bearer credential."""
    import httpx

    from .channels.weixin import load_credentials, login, save_credentials
    from .channels.weixin.auth import acquire_lock

    root = Path.home() / ".Karne" / "weixin"

    async def read_code():
        print(messages.WEIXIN_VERIFICATION_PROMPT, file=sys.stderr)
        return await read_input()

    with acquire_lock(root):
        previous = load_credentials(root)
        if previous and not force:
            return previous
        async with httpx.AsyncClient(trust_env=False) as http:
            credentials = await login(http, previous=previous, read_code=read_code)
        save_credentials(root, credentials)
    print(messages.WEIXIN_LOGIN_SUCCESS, file=sys.stderr)
    return credentials


def main() -> None:
    parser = argparse.ArgumentParser(description=messages.CLI_DESCRIPTION)
    parser.add_argument("--json", action="store_true", help=messages.CLI_JSON_HELP)
    parser.add_argument("--timezone", help=messages.CLI_TIMEZONE_HELP)
    parser.add_argument("--wechat", action="store_true", help=messages.CLI_WECHAT_HELP)
    parser.add_argument("--wechat-login", action="store_true", help=messages.CLI_WECHAT_LOGIN_HELP)
    parser.add_argument(
        "--observe", nargs="?", const=DEFAULT_PORT, type=int, metavar="PORT",
        help=messages.CLI_OBSERVE_HELP.format(port=DEFAULT_PORT),
    )
    parser.add_argument("--max-concurrent-tasks", type=int, default=DEFAULT_MAX_CONCURRENT_TASKS,
                        help=messages.TASK_CONCURRENCY_HELP.format(count=DEFAULT_MAX_CONCURRENT_TASKS))
    args = parser.parse_args()
    if args.max_concurrent_tasks < 1:
        parser.error(messages.TASK_CONCURRENCY_INVALID)
    if args.observe is not None and not 1 <= args.observe <= 65535:
        parser.error(messages.CLI_INVALID_OBSERVE_PORT)
    if args.wechat_login and (args.wechat or args.json or args.observe is not None):
        parser.error(messages.CLI_INVALID_LOGIN_FLAGS)
    if args.wechat and args.json:
        parser.error(messages.CLI_INVALID_JSON_CHANNEL)
    try:
        if args.wechat_login:
            asyncio.run(bind_weixin(force=True))
            raise SystemExit(0)
        options = {"wechat": True} if args.wechat else {}
        if args.max_concurrent_tasks != DEFAULT_MAX_CONCURRENT_TASKS:
            options["max_concurrent_tasks"] = args.max_concurrent_tasks
        raise SystemExit(asyncio.run(converse(
            json_output=args.json, timezone=args.timezone, observe_port=args.observe, **options,
        )))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except PersistenceError:
        raise SystemExit(1) from None
    except Exception as exc:
        import sqlite3

        from .channels.weixin import WeixinError

        if not isinstance(exc, (WeixinError, OSError, sqlite3.Error)) or not (args.wechat or args.wechat_login):
            raise
        code = (exc.code if isinstance(exc, WeixinError) else
                "WEIXIN_STORAGE_FAILED" if isinstance(exc, sqlite3.Error) else type(exc).__name__)
        print(messages.WEIXIN_CHANNEL_FAILED.format(code=code), file=sys.stderr)
        if code in {"WEIXIN_SESSION_EXPIRED", "WEIXIN_LOGIN_REQUIRED"}:
            print(messages.WEIXIN_LOGIN_REQUIRED, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
