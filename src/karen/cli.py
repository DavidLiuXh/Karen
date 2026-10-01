"""Minimal terminal conversation using the existing model transport."""

import asyncio
import json
import os

from dynamic_graph import DynamicGraphEngine, ModelBindings
from dynamic_graph.models.client import ModelCallError
from dynamic_graph.tools import (
    browser_open_local_page_tool,
    file_read_text_tool,
    file_write_text_tool,
    github_trending_tool,
    tavily_search_tool,
    web_fetch_tool,
)
from pydantic import ValidationError

from .agent import Karen
from .intent import IntentRecognizer, IntentSession
from .models import deepseek_client


async def converse() -> int:
    model = deepseek_client()
    engine = DynamicGraphEngine(models=ModelBindings(planner=model, worker=model))
    for tool_factory in (
        file_read_text_tool,
        file_write_text_tool,
        browser_open_local_page_tool,
        web_fetch_tool,
        github_trending_tool,
    ):
        engine.register_tool(tool_factory())
    if os.environ.get("TAVILY_API_KEY", "").strip():
        engine.register_tool(tavily_search_tool())
    else:
        print("Karen：未配置 TAVILY_API_KEY，Tavily 网络搜索暂不可用。")
    agent = Karen(
        intent=IntentRecognizer(model),
        engine=engine,
    )
    session = IntentSession()
    while True:
        try:
            user_input = await asyncio.to_thread(input, "你：")
        except EOFError:
            return 0
        try:
            turn = await agent.advance(session, user_input)
        except (ModelCallError, ValidationError, ValueError, TimeoutError) as exc:
            # Leave the conversation unchanged on a failed assessment.
            code = exc.code if isinstance(exc, ModelCallError) else type(exc).__name__
            print(f"Karen：本轮未完成（{code}），请重新输入。")
            continue
        session = turn.session
        if turn.result is None:
            print("Karen：" + "\n".join(session.questions))
            continue
        print(json.dumps(turn.result.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return (
            0
            if (turn.result.execution_status == "COMPLETED" and turn.result.output_complete)
            else 1
        )


def main() -> None:
    try:
        raise SystemExit(asyncio.run(converse()))
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
