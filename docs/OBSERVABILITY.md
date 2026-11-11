# Karen 运行观测

运行观测是独立的 `karen.observability` 模块。它记录运行过程，不作为 m1/m2/m3 的内容来源，也不改变执行权限。DynamicAgentGraph 源码没有改动。

## 使用

更新环境后，通过启动参数同时运行 Karen 和观测页面：

```bash
uv sync
uv run --env-file .env karen --observe
```

页面右上角支持中文 / English 切换，首次跟随浏览器语言（非中文使用英文），之后记住本浏览器的选择。
切换立即生效，保留当前任务、筛选条件和展开的详情；用户原话、模型输出、标识符和原始 JSON 保持原文。
日期时间按所选语言格式显示，时区仍使用浏览器本地时区。

页面地址默认是 `http://127.0.0.1:8765/`，每两秒刷新。只绑定本机回环地址。流程列表支持明确版本的启用/禁用操作，不提供执行或重放任务的接口。状态修改仅接受同源 JSON POST，拒绝跨站请求。
启动时打印页面地址，使用浏览器访问即可；`--observe 8766` 可以选择其他端口。
页面与当前 CLI 使用同一个观测目录，随 `/exit`、EOF、Ctrl+C 或启动失败关闭并释放端口。
端口绑定失败会提示换端口并结束启动，不会静默显示其他进程的页面。
不加参数时继续记录运行过程，但不启动 HTTP 服务。

Karen 退出后仍可单独运行 `uv run karen-observe --open` 查看历史记录；该独立命令的
`--port` 可以选择其他端口，`--root` 可以指定观测目录。引擎记录目录是该目录的同级 `runs`。

CLI 默认保存位置：

```text
~/.Karne/
  context/                     # 原有上下文记忆，不写入调试日志
  observability/
    traces/<trace_id>.jsonl    # 一轮交互或一次后台处理的事件
    traces/<process_session_id>.health.json
  runs/<run_id>/              # DynamicAgentGraph 的原有记录格式
```

此前 CLI 创建在工作目录 `runs/` 下的旧引擎记录仍保留，但不自动迁移或导入。新的 CLI 使用全局 `~/.Karne/runs`，与观测存储目录分离；观测初始化或写入失败不会使引擎记录目录随之失效。

## 页面能看到什么

- 最近交互列表：输入、时间、交互状态、耗时和降级。一个任务的多轮澄清按 request_id 关联。
- 耗时分解：单独展示本轮精排耗时和同一任务各轮精排的起止时间、耗时与降级状态。使用 `memory.recall.rank` span 的实际耗时，包含内部重试，不把嵌套模型调用重复相加；未完成或缺失的耗时不记为零。
- 等待澄清：从本轮 `needs_clarification` 结束到同一任务下一轮输入之间标出等待区间，并在关键时间线中显示。后台记忆索引不会终止等待区间；没有后续输入时明确显示“尚无后续输入记录”，不据此断言进程仍在等待。记录覆盖不全时仍需结合覆盖警告判断。
- 任务时间线：意图判断、澄清依据、记忆查询理解、融合候选与分数、关系包、精排决策、最终记忆、实际显示回复。
- 模型请求与返回：模型标识、提示词内容及摘要、输入、输出 schema、实际响应、实际用量、错误和耗时。调用没有返回 token 用量时明确保留未知。
- 召回准入与输入统计：`memory.recall.skipped` 展示本轮无需召回的依据；`memory.rerank.input` 展示精排主候选数、记录数、去重来源数和输入字节数。
- 后台记忆处理：入队、原文持久化、提取、核验、替代/冲突决策、提交记忆 ID、embedding 模型/维度与索引完成；失败和重试单独记录。
- 查询分析、精排及召回整体失败的降级事件：memory.degraded 带原有降级码、异常类型和安全错误码。
  契约校验失败记录字段位置与错误类型；缺失 at 或时区时附具体原因。任务详情直接显示原因，
  “错误/降级”筛选也可定位事件；不复制异常消息、响应值或 Pydantic 输入/错误上下文。
- m3 补查：查询范围、来源定位、命中片段、扫描数量和 partial 等返回状态。
- 引擎记录：GoalSpec（包括真正注入的上下文）、执行图与依赖、权限、节点事件、已记录节点产物和最终结果。节点提交与返回分开展示。

查看页面直接读取已有引擎文件；GoalSpec 写入后即可匹配运行，不需要等待 engine.run 返回。尚未终结的引擎记录只能说明“可能仍在执行或已中断”，不能根据缺少结束事件推断成功或确定仍在运行。
节点记录的粒度取决于引擎现有能力：例如节点产物可以显示已记录输出，工具实际输入是否完整可还原取决于执行图绑定及已有来源记录。该版本不增加引擎回调，也不声称能够确定性重放外部工具。
新降级诊断只作用于后续运行，旧记录中的通用错误码不会被回写成推测的失败原因。
CLI 启动时从配置的 DeepSeek 后端读取一次模型目录，按 planner 和 worker 的模型 ID 获取各自的 `max_output_tokens`。首轮请求（含定时任务）使用各自模型的额度，截断后直接报告。模型目录不可用或没有该模型的有效上限时，对应模型沿用本地默认的 32,768 tokens。观测的模型配置通过 `output_token_limit_source` 显示额度来自后端还是本地默认。

现有 worker ModelRequest 不携带 run_id/node_id，因此模型调用通过任务与 execution span 关联，不能精确一一绑定并发节点；节点状态、尝试次数与产物采用引擎原生记录，不根据时间相邻关系猜测绑定。

页面提供独立的“LLM 交互”区，默认收起并显示调用数量，点击标题展开或收起整个区域；每次调用仍可单独展开，标题仅显示模型名称，完整模型配置在详情内以缩进换行的 JSON 展示。自动刷新与语言切换保留展开状态。该区域按 trace_id 和 span_id 配对请求、返回、错误及调用结束事件，显示耗时；展开保留完整已存事件及裁剪标记。缺少请求或返回明确标为记录缺失，不推断仍在运行。此区不受时间线默认隐藏模型事件的筛选影响。

## 事件和因果关系

事件包含 schema_version、event_id、UTC 时间、process_session_id、进程序号、trace_id、span_id、parent_span_id、stage、event_type、status、duration_ms、data 和 coverage。

- conversation_id 关联整段会话；request_id 关联任务；turn_id 区分每次澄清或新输入。
- 每轮输入有独立 trace_id；其节点及模型调用沿用该追踪上下文。
- 后台持久化、派生、重试各有独立 trace；通过 source_event_id、source_trace_id 和 source_span_id 关联触发它的交互。模型或磁盘重试保留 attempt。
- 后台任务重启后恢复时仍有来源事件及任务 ID，但未持久化的来源 span 链接可能缺失。页面可按任务关联，不能伪造原交互的父子 span。
- ContextVar 只用于当前 asyncio 调用链的身份传播；各模块通过注入同一个 Observer 协作，不使用全局可变追踪对象。

公共接口：

```python
from pathlib import Path
from dynamic_graph import EngineConfig
from karen.observability import Observer, ObservedModel

root = Path("/private/tmp/karen-inspection")
observer = Observer(root / "observability", sensitive_values=(api_key,))
await observer.start()
model = ObservedModel(backend_model, observer)

# 将同一 observer 显式注入 IntentRecognizer、ContextMemory 和 Karen。
# 引擎保留其原有 ModelClient/EngineConfig 接口：
engine_config = EngineConfig(runs_dir=root / "runs", sensitive_values=(api_key,))

try:
    turn = await agent.advance(session, user_input)
    # UI 显示以后，用返回的 trace_id 记录实际显示文本。
    agent.record_response(turn.session, displayed_text, trace_id=turn.trace_id)
finally:
    await memory.close()       # 先完成用户记忆的必要持久化
    await observer.close()     # 再排空已经接收的观测事件
```

不注入 observer 的独立 API 保持原有行为；`Observer()` 是禁用的实例。观测生命周期由装配层负责，业务模块不创建存储目录或启动查看服务。

## 如何判断是否符合预期

页面的确定性流程检查与业务验收分开。第一版检查：

1. 返回需要澄清的交互是否没有启动业务执行。
2. 实际 GoalSpec 中的历史对话是否只在 history.status=selected 时注入。
3. 精排失败后的记忆是否标为 unverified，并保留 rerank_rank=null。

完整记录中满足规则显示通过；可见违反显示不符合；记录丢失、坏行或相关证据裁剪显示证据不足；没有发生相应场景显示不适用。
这些检查不能验证关联判断的语义准确率，也不能证明每条业务成功标准已经满足。引擎 COMPLETED/output_complete 与业务验收分别显示，业务验收默认 not_evaluated。

回归测试包含多轮澄清、无关请求隔离、城市变更、精排降级等现有记忆行为，以及新增的实时引擎观察、来源关联、模型错误/取消、并发隔离、脱敏、队列溢出、磁盘失败、坏记录及本地 HTTP 边界。调整提示词或关联判断后应重新运行这些场景，并继续增加实际使用中的失败样例。

## 记录边界

观测只做 best-effort 记录，不能作为用户原文的持久化承诺：

- emit 不等待磁盘或模型；队列最多 512 个事件、4 MiB。溢出或写入失败记为缺口并警告，业务继续。正常关闭排空已接收事件，并保存进程健康记录；强制结束可能丢失尚未写入的事件。
- 单事件 data 上限约 48 KiB，字符串预览 8,000 字节，集合最多 100 项。更大内容明确带裁剪标记；摘要针对脱敏后的内容。完整引擎产物仍通过已有文件查看。
- 已知 API key、凭据字段及常见文本模式在写入前脱敏。CLI 显式传入 DeepSeek/Tavily 凭据；API 调用方负责传入其他已知敏感值。自由文本中的所有隐私内容无法自动可靠识别，观测文件含业务内容，仅在本机使用。
- 文件以 0600 创建，目录以 0700 创建；查看器拒绝记录目录内的符号链接和任意文件路径，页面把所有业务内容作为文本显示。
- 默认列表最多扫描最近 300 个 trace、8 MiB；单 trace 默认读 2 MiB，同任务关联额外扫描最多 8 MiB。大列表使用文件尾部定位终态，并标注裁剪。
- 引擎目录最多扫描最近 300 个 run、8 MiB 目标数据，展示最多八次匹配运行；单 JSON 文件读取上限 1 MiB，单节点产物 64 KiB、最多 30 个。达到限制或文件不可用时显式标记，不能把未显示的记录解释为未执行。
- 第一版不自动删除日志，也不上传外部观测平台。记录规模和长期保留策略需要依据实际使用量再确定。

测试命令：

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check src tests scripts
```

2026-10-03 验证：109 项测试通过，Ruff 检查与格式检查通过；本地浏览器验证目标上下文展开、时间线筛选及多轮任务关联。真实 DeepSeek 合成意图请求返回 ready 和非空 reason，记录实际 input_tokens=2156、output_tokens=127，共 12 个事件、零缺口。安装包构建通过，页面资源和查看命令包含在 wheel 中。

流程发现的 `workflow.generalization_probe` 记录换输入核验请求、参数、输入与编译后的图；它不表示工具执行过或新输入业务成功，仍受观测脱敏和裁剪限制。
## 手动管理可复用流程

展开“可复用流程”中某个定义，点击“启用流程”或“禁用流程”。操作针对该版本即时写入流程索引，后续匹配读取新状态，无需重启 Karen；已开始的执行不受影响。候选、已启用和已暂停版本可以操作，旧的 superseded 版本不提供按钮。启用前检查定义路径、哈希和契约；同一流程的其他已启用版本暂停。手动启用表示用户选择使用该版本，不改写自动核验证据，也不跳过本轮授权与图校验。

更新页面与服务代码后须重启观测服务，刷新页面才能使用新操作。

### 删除流程版本

流程定义详情中的“删除此版本”在确认后移除该版本：从列表隐藏、不再匹配、不可重新启用，其他版本不受影响。删除使用同源 JSON POST `/api/workflows/delete`，只接受 workflow_id 和正整数 version。索引保留 deleted 标记并移除搜索条目，历史定义文件、来源及运行记录保留用于诊断，不清理磁盘文件。后台暂停/结果回写不会恢复删除状态，同一来源的后台重审也不能重新发布已删除的最新版本。已开始的执行不被中断。更新服务代码后需重启 Karen 并刷新页面加载按钮与接口。
