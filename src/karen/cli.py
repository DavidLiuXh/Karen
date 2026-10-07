"""Minimal terminal conversation using the existing model transport."""

import argparse
import asyncio
import json
import os
import sys
import threading
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


async def converse(*, json_output: bool = False, timezone: str | None = None) -> int:
    try:
        session = IntentSession(timezone=timezone) if timezone is not None else IntentSession()
    except (ValueError, ZoneInfoNotFoundError, OSError):
        print("Karen：无法确定有效的用户时区，请使用 --timezone 指定 IANA 时区，如 Asia/Shanghai。")
        return 1
    backend = deepseek_client()
    observer = create_observer()
    await observer.start()
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
        await observer.close()
        return 1
    agent = Karen(
        intent=IntentRecognizer(model, observer=observer),
        engine=engine,
        memory=memory,
        observer=observer,
    )
    try:
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
        finally:
            await observer.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Karen 任务与意图澄清")
    parser.add_argument("--json", action="store_true", help="展示完整执行结果 JSON")
    parser.add_argument("--timezone", help="用户的 IANA 时区，默认读取本机时区")
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(converse(json_output=args.json, timezone=args.timezone)))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except PersistenceError:
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
