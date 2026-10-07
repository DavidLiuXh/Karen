"""Minimal terminal conversation using the existing model transport."""

import argparse
import asyncio
import json
import os
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

from .agent import Karen
from .context import ContextMemory
from .context.contracts import PersistenceError
from .intent import IntentRecognizer, IntentSession
from .models import deepseek_client, memory_embeddings
from .observability import ObservedModel, Observer
from .observability.viewer import DEFAULT_PORT, background_server
from .response import format_result


def create_memory(model, *, rerank_model=None, observer=None) -> ContextMemory:
    return ContextMemory(
        root_dir=Path.home() / ".Karne" / "context",
        model=model,
        rerank_model=rerank_model,
        embeddings=memory_embeddings(),
        observer=observer,
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
        return await PromptSession(history=DummyHistory()).prompt_async("你：")

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
            value = input("你：")
        except BaseException as exc:
            error = exc
        try:
            loop.call_soon_threadsafe(complete, value, error)
        except RuntimeError:
            pass  # The process may finish while its daemon console reader is blocked.

    threading.Thread(target=read, name="karen-console-input", daemon=True).start()
    return await future


async def converse(
    *, json_output: bool = False, timezone: str | None = None, observe_port: int | None = None
) -> int:
    try:
        session = IntentSession(timezone=timezone) if timezone is not None else IntentSession()
    except (ValueError, ZoneInfoNotFoundError, OSError):
        print("Karen：无法确定有效的用户时区，请使用 --timezone 指定 IANA 时区，如 Asia/Shanghai。")
        return 1
    backend = deepseek_client()
    observer = create_observer()
    async with AsyncExitStack() as resources:
        await observer.start()
        resources.push_async_callback(observer.close)
        if observe_port is not None:
            if observer.root_dir is None:
                print("Karen：观测记录目录无法打开，本次未启动对话。", file=sys.stderr)
                return 1
            try:
                server = resources.enter_context(
                    background_server(observer.root_dir, port=observe_port)
                )
            except OSError:
                print(
                    f"Karen：无法启动观测页面，端口 {observe_port} 可能已被占用；"
                    "请使用 --observe 指定其他端口。",
                    file=sys.stderr,
                )
                return 1
            print(
                f"Karen 观测页面：http://127.0.0.1:{server.server_port}/",
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
            print("Karen：未配置 TAVILY_API_KEY，Tavily 网络搜索暂不可用。")
        memory_model = ObservedModel(deepseek_client(thinking=False), observer)
        memory = create_memory(memory_model, rerank_model=model, observer=observer)
        try:
            await memory.start()
        except (OSError, PersistenceError, RuntimeError):
            print("Karen：记忆目录无法安全打开，可能已有进程使用或文件损坏。本次未启动对话。")
            return 1
        try:
            agent = Karen(
                intent=IntentRecognizer(model, observer=observer),
                engine=engine,
                memory=memory,
                observer=observer,
            )
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
                    print(f"Karen：本轮未完成（{code}），请重新输入。")
                    continue
                session = turn.session
                if turn.result is None and turn.response is None:
                    for warning in turn.memory_warnings:
                        print(f"Karen：记忆写入未完成（{warning}）。")
                    print("Karen：" + "\n".join(session.questions))
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
                print(response_text if json_output else "Karen：" + response_text)
                warnings = (
                    *turn.memory_warnings,
                    *agent.record_response(session, response_text, trace_id=turn.trace_id),
                )
                for warning in dict.fromkeys(warnings):
                    print(f"Karen：记忆写入未完成（{warning}）。")
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
                await memory.close()
            except PersistenceError:
                print("Karen：部分对话未能持久化，退出失败。")
                raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Karen 任务与意图澄清")
    parser.add_argument("--json", action="store_true", help="展示完整执行结果 JSON")
    parser.add_argument("--timezone", help="用户的 IANA 时区，默认读取本机时区")
    parser.add_argument(
        "--observe", nargs="?", const=DEFAULT_PORT, type=int, metavar="PORT",
        help=f"同时启动本地观测页面，默认端口 {DEFAULT_PORT}，可指定其他端口",
    )
    args = parser.parse_args()
    if args.observe is not None and not 1 <= args.observe <= 65535:
        parser.error("--observe 端口必须在 1–65535 之间")
    try:
        raise SystemExit(asyncio.run(converse(
            json_output=args.json, timezone=args.timezone, observe_port=args.observe
        )))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except PersistenceError:
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
