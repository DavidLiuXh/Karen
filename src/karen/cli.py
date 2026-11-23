"""Minimal terminal conversation using the existing model transport."""

import argparse
import asyncio
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from contextlib import AsyncExitStack, nullcontext
from dataclasses import replace
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

from dynamic_graph import DynamicGraphEngine, EngineConfig, ModelBindings
from dynamic_graph.tools import (
    browser_open_local_page_tool,
    file_read_text_tool,
    file_write_text_tool,
    tavily_search_tool,
)
from prompt_toolkit import PromptSession
from prompt_toolkit.application import get_app
from prompt_toolkit.formatted_text import FormattedText, to_formatted_text
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from rich.console import Console
from rich.text import Text

from .agent import Karen
from .context import ContextMemory
from .context.contracts import PersistenceError
from .intent import IntentRecognizer, IntentSession
from .message import messages
from .model.models import (
    KarenPlannerModel,
    configure_execution_output_limits,
    deepseek_client,
    memory_embeddings,
)
from .model.response import terminal_markdown
from .observability import ObservedModel, Observer
from .observability.viewer import DEFAULT_PORT, background_server
from .schedule import ScheduledTasks
from .task import DEFAULT_MAX_CONCURRENT_TASKS, TaskManager
from .task.console import run_console
from .task.resources import ExecutionResources
from .task.storage import TaskStore
from .task.terminal_rendering import (
    READING_WIDTH,
    TERMINAL_THEME,
    ReplyMarkdown,
    render_notice,
    render_reply,
)
from .tools.file_range import READ_RANGE_BYTES, file_read_text_range_tool
from .tools.git import git_clone_tool
from .tools.gmail import GmailClient, GmailCredentials
from .tools.gmail_tools import gmail_tools
from .tools.local import file_create_directory_tool, file_edit_text_tool
from .tools.run_artifacts import run_artifacts_tool
from .tools.web import web_read_page_tool
from .workflow import ReusableWorkflows

# Keep one history for the lifetime of the CLI process so each prompt can
# browse commands entered at earlier prompts.
_input_history = InMemoryHistory()


def _render_terminal_markdown(console, text, *, use_glow):
    markdown = terminal_markdown(text)
    rendered_markdown = ReplyMarkdown(markdown)
    # Rich aligns wrapped list text after the marker, including nested lists.
    if any(token.type in {"ordered_list_open", "bullet_list_open"} for token in rendered_markdown.parsed):
        console.print(rendered_markdown)
        return
    glow = shutil.which("glow") if use_glow and not console.no_color else None
    if glow:
        env = os.environ.copy()
        env["CLICOLOR_FORCE"] = "1"
        background = env.get("COLORFGBG", "").rsplit(";", 1)[-1]
        style = "light" if background in {"7", "15"} else "dark"
        try:
            rendered = subprocess.run(
                [glow, "--pager=false", "--tui=false", "-s", style, "-w", str(console.width), "-"],
                input=markdown, capture_output=True, text=True, encoding="utf-8",
                env=env, timeout=2, check=True,
            )
            if rendered.stdout.strip():
                print(rendered.stdout, end="" if rendered.stdout.endswith("\n") else "\n")
                return
        except (OSError, UnicodeError, subprocess.SubprocessError):
            pass
    console.print(rendered_markdown)


class TerminalProgress:
    def __init__(self):
        self.tasks = {}
        self.app = None
        self.focus = None
        self.title = ""

    def toolbar(self):
        width = max(1, min(READING_WIDTH, get_app().output.get_size().columns) - 2)
        hint = messages.CLI_INPUT_HINT
        current = messages.CLI_CURRENT_TASK.format(title=self.title, task_id=self.focus) if self.focus else ""
        if current and width >= 60:
            label = Text(current)
            label.truncate(max(1, width - get_cwidth(hint) - 3), overflow="ellipsis")
            text = label.plain + " · " + hint
        else:
            text = hint
        return FormattedText([("", "  " + text)])

    def accept(self, task_id):
        if task_id not in self.tasks:
            self.tasks[task_id] = messages.TASK_ACCEPTED.format(task_id=task_id)

    def update(self, stage, task_id):
        label = messages.TASK_PROGRESS_STAGES.get(stage)
        if label is not None:
            self.tasks.pop(task_id, None)
            self.tasks[task_id] = messages.TASK_PROGRESS.format(task_id=task_id, stage=label)
            if self.app is not None:
                self.app.invalidate()

    def finish(self, task_id):
        if self.tasks.pop(task_id, None) is not None and self.app is not None:
            self.app.invalidate()

    def status_line(self):
        if not self.tasks:
            return ""
        task_id = self.focus if self.focus in self.tasks else next(reversed(self.tasks))
        text = self.tasks[task_id]
        if len(self.tasks) > 1:
            text += messages.CLI_OTHER_RUNNING.format(count=len(self.tasks) - 1)
        width = max(1, min(READING_WIDTH, get_app().output.get_size().columns) - 4)
        while text and get_cwidth(text) >= width:
            text = text[:-1]
        return text


def display_response(text: str, *, json_output: bool = False, use_glow: bool = False, notice: bool = False) -> None:
    """Render terminal replies while keeping redirected and JSON output unchanged."""
    if json_output:
        print(text)
    elif sys.stdout.isatty() and os.environ.get("TERM") != "dumb":
        console = Console(file=sys.stdout, theme=TERMINAL_THEME)
        if text == messages.CLI_WELCOME:
            console.print()
            console.print(Text("  " + messages.CLI_TITLE, style="bold magenta"))
            render_notice(console, text)
        elif text == messages.TASK_HELP:
            render_notice(console, text.replace("；", "\n"))
        elif notice:
            render_notice(console, text)
        elif use_glow:
            _render_terminal_markdown(console, text, use_glow=True)
        else:
            render_reply(console, terminal_markdown(text))
    else:
        print(messages.KAREN_PREFIX + text)


def update_terminal_focus(progress, manager, task_id):
    task = manager.tasks.get(task_id)
    progress.focus = task_id
    progress.title = task.title if task is not None else ""
    if progress.app is not None:
        progress.app.invalidate()


def display_task_response(event, text, *, title, deliveries, schedule=None):
    render_reply(
        Console(file=sys.stdout, theme=TERMINAL_THEME), terminal_markdown(text),
        task_id=event.task_id, title=title, state=event.state,
        deliveries=deliveries, schedule=schedule,
    )


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


async def read_input(*, progress=None) -> str:
    """Cancel terminal editing cleanly; keep redirected input compatible with input()."""
    if sys.stdin.isatty():
        # Async editing restores terminal modes before cancellation or Ctrl+C propagates.
        prompt = messages.INPUT_PROMPT
        if os.environ.get("TERM") != "dumb":
            style = "" if "NO_COLOR" in os.environ else "bold ansicyan"
            prompt = FormattedText([(style, messages.CLI_INPUT_PROMPT)])

        def prompt_with_progress():
            status = progress.status_line()
            width = max(1, min(READING_WIDTH, get_app().output.get_size().columns) - 4)
            muted = "" if "NO_COLOR" in os.environ else "fg:ansibrightblack"
            prefix = [(muted, "  " + "─" * width + "\n")]
            if status:
                prefix.append(("", "  " + status + "\n"))
            return FormattedText(prefix + [("", "  ")] + list(to_formatted_text(prompt)))

        options = {}
        if progress is not None:
            options["style"] = Style.from_dict({"bottom-toolbar": "noreverse bg:default fg:default" + (" fg:ansibrightblack" if "NO_COLOR" not in os.environ else "")})
        session = PromptSession(history=_input_history, **options)
        if progress is not None:
            progress.app = session.app
        try:
            if progress is not None:
                return await session.prompt_async(prompt_with_progress, bottom_toolbar=progress.toolbar)
            return await session.prompt_async(prompt)
        finally:
            if progress is not None:
                progress.app = None

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


async def register_execution_tools(engine, resources, resources_guard):
    for tool_factory in (
        file_read_text_tool,
        file_read_text_range_tool,
        file_write_text_tool,
        file_edit_text_tool,
        file_create_directory_tool,
        browser_open_local_page_tool,
    ):
        if tool_factory is file_read_text_tool:
            definition = file_read_text_tool(max_bytes=READ_RANGE_BYTES)
            definition = replace(
                definition,
                description=definition.description + " For large files or unknown sizes, "
                "use file.read_text_range to read only the needed parts.",
            )
        else:
            definition = tool_factory()
        engine.register_tool(resources_guard.file_tool(
            definition, write=tool_factory in {file_write_text_tool, file_edit_text_tool, file_create_directory_tool},
        ))
    engine.register_tool(resources_guard.independent_read(web_read_page_tool()))
    engine.register_tool(resources_guard.file_tool(git_clone_tool(), write=True))
    engine.register_tool(resources_guard.file_tool(
        run_artifacts_tool(engine.config.runs_dir), root=engine.config.runs_dir,
    ))
    if os.environ.get("TAVILY_API_KEY", "").strip():
        engine.register_tool(resources_guard.independent_read(tavily_search_tool()))
    await register_gmail_tools(engine, resources, resources_guard)


async def converse(
    *, json_output: bool = False, timezone: str | None = None, observe_port: int | None = None,
    wechat: bool = False, cli: bool = False, use_glow: bool = False,
    max_concurrent_tasks: int = DEFAULT_MAX_CONCURRENT_TASKS,
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
        task_store = None
        if not wechat or cli:
            try:
                task_store = TaskStore(Path.home() / ".Karne" / "tasks")
            except (OSError, sqlite3.Error):
                print(messages.TASK_STORAGE_UNAVAILABLE, file=sys.stderr)
                return 1
            resources.callback(task_store.close)
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
        planner = deepseek_client(reasoning_effort="medium")
        output_limit = await configure_execution_output_limits(planner, backend)
        model = ObservedModel(backend, observer)
        config = EngineConfig(
            runs_dir=Path.home() / ".Karne" / "runs" if observer.root_dir else Path("runs"),
            max_output_tokens=output_limit,
            sensitive_values=observer.sensitive_values,
        )
        models = ModelBindings(
            planner=KarenPlannerModel(ObservedModel(planner, observer)),
            worker=model,
        )
        Path(config.runs_dir).mkdir(parents=True, exist_ok=True)
        resources_guard = ExecutionResources(observer)
        engine = DynamicGraphEngine(config=config, models=models)
        weixin_engine = DynamicGraphEngine(config=config, models=models) if wechat and cli else engine
        await register_execution_tools(engine, resources, resources_guard)
        if weixin_engine is not engine:
            await register_execution_tools(weixin_engine, resources, resources_guard)
        if not os.environ.get("TAVILY_API_KEY", "").strip():
            print(messages.TAVILY_UNAVAILABLE)
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

            def create_agent(channel_engine, store=None):
                return Karen(
                    intent=IntentRecognizer(model, observer=observer, capabilities=channel_engine.list_capabilities),
                    engine=channel_engine,
                    memory=memory,
                    observer=observer,
                    workflows=workflows,
                    resources=resources_guard,
                    snapshot_deliveries=store.snapshot if store is not None else None,
                    recover_deliveries=store.recover_deliveries if store is not None else None,
                    link_run=store.link_run if store is not None else None,
                )

            channel = None
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
                weixin_engine.register_tool(resources_guard.file_tool(prepare_file_tool(store)))
                http = await resources.enter_async_context(httpx.AsyncClient(trust_env=False))
                channel = WeixinChannel(
                    agent=create_agent(weixin_engine),
                    client=ILinkClient(http, token=credentials.token,
                                       base_url=credentials.base_url),
                    store=store, credentials=credentials, initial_session=session, observer=observer,
                    max_concurrent_tasks=max_concurrent_tasks,
                    schedule_model=memory_model,
                )
                print(messages.WEIXIN_STARTED, file=sys.stderr)
                if not cli:
                    await channel.run()
                    return 0
            from prompt_toolkit.patch_stdout import patch_stdout

            session = session.model_copy(update={"user_context": {
                **session.user_context, "cli_file_deliveries": True,
            }})
            manager = TaskManager(
                create_agent(engine, task_store), max_concurrent=max_concurrent_tasks,
                observer=observer, store=task_store,
            )
            schedules = create_schedules(manager, session, memory_model)
            if schedules is not None:
                resources.callback(schedules.close)
            interactive = sys.stdout.isatty()
            show_progress = (
                interactive and sys.stdin.isatty() and not json_output
                and os.environ.get("TERM") != "dumb"
            )
            progress = TerminalProgress() if show_progress else None

            async def console():
                with (
                    patch_stdout(raw=True) if interactive else nullcontext(),
                    observer.listen_stages(progress.update) if progress is not None else nullcontext(),
                ):
                    return await run_console(
                        manager, session,
                        read_input=partial(read_input, progress=progress) if progress is not None else read_input,
                        on_task_accepted=progress.accept if progress is not None else None,
                        on_task_finished=progress.finish if progress is not None else None,
                        display=partial(display_response, use_glow=use_glow, notice=True),
                        display_task=display_task_response if interactive and not json_output and os.environ.get("TERM") != "dumb" else None,
                        on_focus_changed=partial(update_terminal_focus, progress, manager) if progress is not None else None,
                        json_output=json_output,
                        schedules=schedules,
                    )

            if channel is None:
                return await console()
            weixin_task = asyncio.create_task(channel.run())
            console_task = asyncio.create_task(console())
            try:
                done, _ = await asyncio.wait(
                    {weixin_task, console_task}, return_when=asyncio.FIRST_COMPLETED,
                )
                if weixin_task in done:
                    await weixin_task
                return await console_task if console_task in done else 0
            finally:
                weixin_task.cancel()
                console_task.cancel()
                await asyncio.gather(weixin_task, console_task, return_exceptions=True)
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
    parser.add_argument("--cli", action="store_true", help=messages.CLI_TERMINAL_HELP)
    parser.add_argument("--wechat", action="store_true", help=messages.CLI_WECHAT_HELP)
    parser.add_argument("--wechat-login", action="store_true", help=messages.CLI_WECHAT_LOGIN_HELP)
    parser.add_argument(
        "--observe", nargs="?", const=DEFAULT_PORT, type=int, metavar="PORT",
        help=messages.CLI_OBSERVE_HELP.format(port=DEFAULT_PORT),
    )
    parser.add_argument("--max-concurrent-tasks", type=int, default=DEFAULT_MAX_CONCURRENT_TASKS,
                        help=messages.TASK_CONCURRENCY_HELP.format(count=DEFAULT_MAX_CONCURRENT_TASKS))
    parser.add_argument("--no-glow", action="store_true", help=messages.CLI_NO_GLOW_HELP)
    args = parser.parse_args()
    if args.max_concurrent_tasks < 1:
        parser.error(messages.TASK_CONCURRENCY_INVALID)
    if args.observe is not None and not 1 <= args.observe <= 65535:
        parser.error(messages.CLI_INVALID_OBSERVE_PORT)
    if args.wechat_login and (args.wechat or args.cli or args.json or args.observe is not None):
        parser.error(messages.CLI_INVALID_LOGIN_FLAGS)
    if args.wechat and args.json:
        parser.error(messages.CLI_INVALID_JSON_CHANNEL)
    try:
        if args.wechat_login:
            asyncio.run(bind_weixin(force=True))
            raise SystemExit(0)
        options = {"wechat": True} if args.wechat else {}
        if args.cli:
            options["cli"] = True
        if args.no_glow:
            options["use_glow"] = False
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
