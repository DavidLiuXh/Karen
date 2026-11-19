"""Observable layout, provenance and input ownership in the terminal UI."""
import io
from types import SimpleNamespace

import pytest
from rich.console import Console
from rich.text import Text

from karen import IntentSession, TaskTurn, cli
from karen.message import messages
from karen.task import TaskManager
from karen.task.console import run_console
from karen.task.terminal_rendering import TERMINAL_THEME, render_reply


@pytest.mark.parametrize('width', [24, 40, 80, 160])
@pytest.mark.parametrize('no_color', [True, False])
def test_structured_reply_preserves_text_paths_and_reading_width(width, no_color):
    output = io.StringIO()
    console = Console(file=output, width=width, theme=TERMINAL_THEME, no_color=no_color,
                      force_terminal=True, color_system='standard', _environ={'TERM': 'xterm'})
    path = '/tmp/研究报告/很长的存储路径/' + 'report-' * 8 + '.md'
    render_reply(console, '结论：内容完整。\n\n## 依据\n\n- 中文换行与缩进。\n\n```python\nprint(42)\n```',
                 task_id='abc123', title='调研 [red] 不是样式', state='completed', deliveries=[
                     {'kind': 'file', 'name': '研究报告.md', 'available': True, 'path': path}])
    plain = Text.from_ansi(output.getvalue()).plain
    compact = ''.join(plain.split())
    assert path in compact
    assert all(text in compact for text in ['内容完整', '中文换行与缩进', 'print(42)', '交付文件', 'abc123'])
    assert plain.count('Karen') == 1
    assert max(Text(line).cell_len for line in plain.splitlines()) <= min(width, 100)
    assert '╭' not in plain  # Long messages remain scrollback-friendly.
    if no_color:
        assert '\x1b[36m' not in output.getvalue()


async def test_console_delivers_structured_results_and_notices_separately():
    class Agent:
        async def advance(self, session, text, **kwargs):
            return TaskTurn(session.model_copy(update={'reply': '回答中的 [red] 是原文'}))

        def record_response(self, *args, **kwargs):
            return ()

    manager = TaskManager(Agent())
    notices, rendered, focuses = [], [], []
    inputs = iter(['/new 带标题的请求', '/help'])

    async def read():
        await manager.drain()
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError from None

    code = await run_console(manager, IntentSession(), read_input=read,
                             display=lambda text, **kwargs: notices.append(text),
                             display_task=lambda event, text, **kwargs: rendered.append((event, text, kwargs)),
                             on_focus_changed=focuses.append)
    assert code == 0 and len(rendered) == 1
    event, text, details = rendered[0]
    assert details['title'] == '带标题的请求' and text == '回答中的 [red] 是原文'
    assert event.task_id == focuses[0]
    assert notices[0] == messages.CLI_WELCOME and messages.TASK_HELP in notices
    assert not any('回答中的' in notice for notice in notices)


def test_progress_keeps_selected_task_visible_among_background_updates(monkeypatch):
    monkeypatch.setattr(cli, 'get_app', lambda: SimpleNamespace(
        output=SimpleNamespace(get_size=lambda: SimpleNamespace(columns=120))))
    progress = cli.TerminalProgress()
    progress.focus, progress.title = 'first', '当前研究'
    progress.accept('first')
    progress.update('memory.recall', 'first')
    progress.update('execution', 'second')
    assert 'first' in progress.status_line() and '另有 1 项' in progress.status_line()
    assert '当前研究' in progress.toolbar()[0][1]
    progress.finish('first')
    assert 'second' in progress.status_line()
    assert '当前研究' in progress.toolbar()[0][1]
    progress.finish('second')
    assert progress.status_line() == ''
