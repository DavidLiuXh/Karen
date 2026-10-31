"""Minimal terminal conversation using the existing model transport."""

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import threading
from contextlib import AsyncExitStack
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

from dynamic_graph import DynamicGraphEngine, EngineConfig, ModelBindings
from dynamic_graph.models.client import ModelCallError
from dynamic_graph.tools import (
    browser_open_local_page_tool,
    file_read_text_tool,
    file_write_text_tool,
    tavily_search_tool,
    web_fetch_tool,
)
from prompt_toolkit import PromptSession
from prompt_toolkit.history import DummyHistory
from pydantic import ValidationError
from rich.console import Console
from rich.markdown import Markdown

from . import messages
from .agent import Karen
from .context import ContextMemory
from .context.contracts import PersistenceError
from .intent import IntentRecognizer, IntentSession
from .model.models import deepseek_client, memory_embeddings
from .model.response import format_result
from .observability import ObservedModel, Observer
from .observability.viewer import DEFAULT_PORT, background_server
from .workflow import ReusableWorkflows


def display_response(text: str, *, json_output: bool = False) -> None:
    """Render terminal replies while keeping redirected and JSON output unchanged."""
    if json_output:
        print(text)
    elif sys.stdout.isatty() and os.environ.get("TERM") != "dumb":
        console = Console(file=sys.stdout)
        console.print(messages.KAREN_PREFIX, markup=False)
        console.print(Markdown(text))
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


async def read_input() -> str:
    """Cancel terminal editing cleanly; keep redirected input compatible with input()."""
    if sys.stdin.isatty():
        # Async editing restores terminal modes before cancellation or Ctrl+C propagates.
        # Do not retain user requests in the line editor's history.
        return await PromptSession(history=DummyHistory()).prompt_async(messages.INPUT_PROMPT)

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


async def converse(
    *, json_output: bool = False, timezone: str | None = None, observe_port: int | None = None,
    wechat: bool = False,
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
        for tool_factory in (
            file_read_text_tool,
            file_write_text_tool,
            browser_open_local_page_tool,
            web_fetch_tool,
        ):
            engine.register_tool(tool_factory())
        if os.environ.get("TAVILY_API_KEY", "").strip():
            engine.register_tool(tavily_search_tool())
        else:
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
            agent = Karen(
                intent=IntentRecognizer(model, observer=observer),
                engine=engine,
                memory=memory,
                observer=observer,
                workflows=workflows,
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
                engine.register_tool(prepare_file_tool(store))
                http = await resources.enter_async_context(httpx.AsyncClient(trust_env=False))
                channel = WeixinChannel(
                    agent=agent, client=ILinkClient(http, token=credentials.token,
                                                  base_url=credentials.base_url),
                    store=store, credentials=credentials, initial_session=session, observer=observer,
                )
                print(messages.WEIXIN_STARTED, file=sys.stderr)
                await channel.run()
                return 0
            exit_code = 0
            while True:
                try:
                    user_input = await read_input()
                except EOFError:
                    return exit_code
                if user_input.strip() == "/exit":
                    return exit_code
                try:
                    turn = await agent.advance(session, user_input)
                except (ModelCallError, ValidationError, ValueError, TimeoutError) as exc:
                    # Leave the conversation unchanged on a failed assessment.
                    code = exc.code if isinstance(exc, ModelCallError) else type(exc).__name__
                    print(messages.CLI_TURN_FAILED.format(code=code))
                    continue
                session = turn.session
                if turn.result is None and turn.response is None:
                    for warning in turn.memory_warnings:
                        print(messages.MEMORY_WRITE_FAILED.format(warning=warning))
                    if json_output:
                        print(messages.KAREN_PREFIX + "\n".join(session.questions))
                    else:
                        display_response("\n".join(session.questions))
                    continue
                if turn.response is not None:
                    response_text = (
                        json.dumps(
                            {
                                "outcome": "cancelled"
                                if session.routing.handling == "cancel"
                                else "replied",
                                "answer": turn.response,
                                "routing": session.routing.model_dump(mode="json"),
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                        if json_output
                        else turn.response
                    )
                else:
                    response_text = (
                        json.dumps(turn.result.model_dump(mode="json"), ensure_ascii=False, indent=2)
                        if json_output
                        else format_result(turn.result)
                    )
                display_response(response_text, json_output=json_output)
                warnings = (
                    *turn.memory_warnings,
                    *agent.record_response(session, response_text, trace_id=turn.trace_id),
                )
                for warning in dict.fromkeys(warnings):
                    print(messages.MEMORY_WRITE_FAILED.format(warning=warning))
                exit_code = (
                    0
                    if (
                        turn.response is not None
                        or (turn.result.execution_status == "COMPLETED" and turn.result.output_complete)
                    )
                    else 1
                )
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
    args = parser.parse_args()
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
