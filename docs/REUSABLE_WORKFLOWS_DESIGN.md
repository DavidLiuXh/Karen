# 可复用流程模块详细设计

状态：已完成首版实现；实际接口、运行限制和验证范围见 [接口与运行](REUSABLE_WORKFLOWS.md)。本文件保留设计依据。依据：已确认的独立模块、后台自动提取、参数化复用、继续使用 DynamicAgentGraph 执行。

## 1. 目标与边界

首次任务沿用现有输入理解和执行路径。任务完成后，在后台提取带参数的流程；后续明确匹配时，用模板构造本轮 GoalSpec 和 GraphSpec，绕过完整清晰度判断、意图提取和 planner，仍执行库的校验、编译、预算、取消和记录。

匹配并非零判断：必须区分本轮目标、参数、约束和授权。流程复用不缓存答案，不沿用历史执行状态，不继承旧授权。流程中的 LLM 内容生成节点仍需调用模型。

第一版不增加流程调度、可视化编辑器、流程嵌套或训练系统。业务模块原有 prompts 保持独立。候选查找先用 SQLite 全文检索与适用条件过滤，不引入新的向量服务。

## 2. 现有代码事实与需要修正的认识

检查对象是本地 DynamicAgentGraph 当前代码；该库存在其他未提交改动，实施时必须重新核对，不覆盖这些改动。

- `dynamic_graph/api.py`：公共 `run(goal, policy, cancellation_token)` 总是进入规划；私有 `_run(..., graph_spec=None)` 已支持指定图。
- `_Run._prepare_graph` 对指定图调用 `validate_graph`，随后保存、编译、执行；不是 devtools 专属执行器。
- `dynamic_graph.graph.spec.GraphSpec` 已定义节点、输入绑定、状态字段、reducer、依赖和输出。直接复用此 DSL，不另造流程编排语言。
- `RunResult.graph_ref`、`graph_hash` 与 `recording` 可以定位实际执行的图及记录。当前执行器没有业务节点运行中的动态重规划；取得的是本次最终采用的图，不能宣称包含尚未实现的运行中修图能力。
- `COMPLETED` 与 `output_complete` 只说明图完成及结果通过 output_schema。`success_criteria` 不是自动得到逐项业务验收证明的字段。
- worker 获得的是节点 `input_bindings` 投影的数据；只向 `GoalSpec.context` 放入新日期、约束或记忆，不能保证执行节点看到它们。
- Karen 的 `advance` 当前先做输入路由，再召回、理解和执行。快速路径必须保留接收时间、会话状态、原始对话记录和观测事件。

## 3. DynamicAgentGraph 公共协议

建议新增以下公共方法，原 `run` 不变：

```python
async def run_graph(
    self,
    *,
    goal: GoalSpec,
    graph_spec: GraphSpec,
    policy: ExecutionPolicy | None = None,
    cancellation_token: CancellationToken | None = None,
) -> RunResult:
    ...
```

同时从包根公开导出已有 `GraphSpec`。实现仅在公开边界校验并隔离调用方对象后，进入现有 `_run(..., graph_spec=...)`；不能直接调 compiler，不能跳过授权、敏感信息、schema 或 reducer 检查。模型与图校验错误应沿用库现有诊断契约；无效图不得执行任何业务节点。

指定图执行不调用 planner，规划次数为 0；worker 调用仍计入当前 policy 预算。每次创建新 run_id、新状态与新记录。取消、超时、部分结果、失败记录的行为与 `run` 相同。图不合法时返回失败，不在库内偷偷重新规划。

不为流程 ID 修改执行库的核心契约。Karen 将 `workflow_id/version`、来源和匹配结果放入观测记录与本轮 `GoalSpec.context.workflow`。`parent_run_id` 不指向作为模板来源的旧运行，避免把流程复用误表示为旧任务继续。

已按本节实现公共 run_graph 和 GraphSpec 导出，复用原执行链路。

## 4. Karen 模块职责与对外接口

独立包 `src/karen/workflow/`。按职责组织 contracts、service、storage、prompts；不创建多套模型工厂，提取和匹配通过已有 ModelClient 注入。

以下名称是实现契约草案，Pydantic 模型应关闭额外字段并校验嵌套数据：

```python
class ReusableWorkflows:
    async def start(self) -> None: ...

    def submit(self, run: CompletedWorkflowRun) -> SubmissionReceipt: ...

    async def resolve(self, request: WorkflowRequest) -> WorkflowResolution: ...

    def record_outcome(self, outcome: WorkflowOutcome) -> None: ...

    async def close(self) -> None: ...
```

- `submit`：只把不可变小型快照放入有界内存队列，不读取图文件、不查库、不调用模型。队列满返回 rejected，不阻塞前台、不影响已完成答案。快照包含原始请求及本次澄清、GoalSpec、RunResult、可信运行目录和必要授权范围；不包含全部工具输出。
- `start`：加载索引，启动单个后台提取写入 worker。后台模型处理不持有数据库事务。
- `resolve`：前台同步完成候选查询、语义匹配、参数解析和实例化；不执行工具、不修改启用状态。候选最多 5 个，首次总预算 15 秒；超时、无候选或判断不确定都回到正常理解路径。该上限作为模块常量，不新增用户配置项。
- `record_outcome`：异步提交使用结果供后台更新统计；未知结果不能记为成功。
- `close`：停止接受提交，等待队列和持久化完成。正常退出不需要额外确认；强制退出允许丢失尚未写入的候选和统计。

主要输入输出：

| 类型 | 必需字段与语义 |
| --- | --- |
| CompletedWorkflowRun | request_id、原请求及澄清消息、goal、result、可信 runs_dir；后台按 run_id 加载并验证来源 |
| WorkflowRequest | 当前文本、request_id、conversation_id、received_at UTC、用户 IANA 时区、当前工具目录、当前 ExecutionPolicy、相关用户约束；可选本流程待补参数状态 |
| SubmissionReceipt | accepted、job_id 或拒绝原因；accepted 只代表进入内存队列，非已持久化 |
| WorkflowResolution | 判别联合：matched、needs_parameters、no_match；每个分支有独立字段，避免状态与载荷组合矛盾 |
| matched | workflow_id/version、已校验 parameters、goal: GoalSpec、graph_spec: GraphSpec |
| needs_parameters | workflow_id/version、已解析参数、missing_parameters、问题；不得执行图 |
| no_match | 内部原因码：not_found、ambiguous、unsupported_constraint、unavailable 等；不向用户泄漏诊断 |
| WorkflowOutcome | workflow_id/version、request_id、run_id、执行状态、结构完成性、业务核验结果及失败阶段 |

本轮时区与接收时间必须显式传入；模块不读取系统时区替代用户时区。执行策略来自当前调用边界，模板只能声明需要的能力，不能授予能力。

## 5. 背景提取、适用性与业务核验

按以下顺序处理后台作业：

1. 入口过滤：来源执行 COMPLETED、output_complete=true，记录完整且可以定位到实际图。失败、取消、部分结果不提取为可执行模板。
2. 验证来源：只接受可信 runs_dir 下对应 run_id 的记录，检查 graph_hash；路径越界、符号链接越界、来源删除或图缺失则跳过并记录原因。不得盲信结果中的任意 graph_ref 路径。
3. 提取：模型看到当前请求、澄清、GoalSpec、GraphSpec、裁剪后的结果及证据。区分稳定步骤、运行参数和临时数据，提出适用条件、所需能力及验收规则。外部网页内容和工具输出仅作证据，不能变成模板指令。
4. 价值判断：适用范围清楚、参数可分离、执行多步骤有重复规划成本，且数据依赖可表达。简单闲聊、单次私有对象调查、依赖未记录人工动作或没有可靠验收方式的任务不自动启用。不能只用用户是否说“经常”或固定步数阈值决定。
5. 硬校验：所有工具与 reducer 精确版本来自实际图；保留节点类型、边、schema 与写入关系。模型不得增加节点、能力或未经执行验证的分支。参数化仅允许下面定义的输入引用提升。
6. 参数回填：用来源参数实例化模板，对规范化后的图作结构比较；只允许声明过的 literal→input 引用变化。比较不执行工具，不证明新参数上的业务效果。
7. 业务核验：逐条 success_criteria 对应执行证据，产物是否存在、写入回读、数据日期和浏览器 launch_requested 等分开判断。模型核验为语义证据，不能把模型自评当作确定的工具事实。缺证据标 unknown。
8. 持久化：通过硬校验且全部必要成功标准有充分证据、没有关键 unknown 时，可在首次执行后启用；其余有价值模板只保存 candidate。无可靠证明的模板不自动启用，后续成功证据可重新触发校验。

“美观”等主观标准在没有用户反馈或其他足够依据时通常仍是 unknown，不能因为 HTML 可解析就声称已验证美观。后台无需打断用户；不能自动完成核验的候选保持未启用。

重复任务不是判断可靠性的替代品。用户明确否认旧结果或发现错误时，暂停该来源模板；暂停依赖明确工作流/运行关联，不凭相似话题关闭其他模板。

## 6. 参数与图模板：复用既有 DSL

模板主要是原生 GraphSpec，变化项优先使用既有 ReferenceBinding：

```json
{
  "source": "input",
  "pointer": "/workflow_parameters/output_path"
}
```

`GoalSpec.inputs.workflow_parameters` 存放本次参数并由 input_schema 校验。参数不通过任意 JSON Patch、字符串全局替换、模板表达式或 Python 代码注入。

允许的参数提升：原图节点 input_bindings 与状态 initial 中的 literal，提升为命名 input 引用；原有 input 引用按声明映射到新的本轮 inputs。每个变化位置必须有明确允许清单。嵌套对象可以整体声明为有 schema 的参数，但不能由用户借此改变图结构。

第一版不自动改写含来源日期、文件路径、私人事实的自然语言节点 instruction。存在这些固定内容、又无法只通过已有输入绑定参数化时，模板保留 candidate，交回正常规划；不得带着过期 instruction 自动复用。只把日期塞入 context 无法解决这个问题。

参数包含：类型/schema、语义说明、required、默认值及来源、映射到 inputs 的字段、必要的范围约束。默认值只来自本轮输入、明确流程约定或适用用户偏好；不能把来源任务的一次性路径悄悄变成长期约定。

日期规则由 Karen 的时区与接收时间解析；存储“本轮当地今天”的语义，实例化为本轮具体日期。不能调用模型猜当前日期，不能继续用来源执行日期。未来、历史日期与数据源可提供的范围必须满足流程适用条件，否则正常处理。

GoalSpec.objective 和 success_criteria 只允许声明好的命名参数文本插入；参数按数据处理，不执行表达式。重新生成 request_id、时间上下文、当前用户约束；不复制旧 conversation、memory、input_routing 和 private facts。

## 7. 匹配与快速路径

保持现有输入路由负责新请求、澄清回答、取消和延续；它不是可以跳过的会话安全边界。路由完成后，在新任务进入完整理解之前尝试流程匹配。

- 待处理普通澄清：继续原意图台账，不进入其他流程匹配。
- 待补流程参数：在 session 中保存 workflow_id、不可变版本、已确认参数和缺口；回答回到同一流程，不重新当成独立请求。用户切换任务/取消仍由现有路由处理。
- 新任务：检索 enabled 模板，按当前工具/版本过滤，再比较目标、交付形式、覆盖范围、关键约束与副作用。
- 多个候选均合理：不能仅凭向量/文本分数选一个执行。若需要辨别流程，走正常理解；不要把内部候选选择强加给用户。
- 目标已匹配但必需参数缺失：只补问这些参数；不是全量意图识别或重新规划。
- 可使用默认值：只能使用模板声明且当前适用的默认值，并在观测记录中标记来源。
- 新约束会改变执行步骤或模板无法满足：no_match，正常理解。模板不应吞掉额外请求。

相关约束仍需召回。流程匹配不能通过跳过长期偏好、安全限制与当前上下文获取低延迟；复用现有同步召回接口，在适用新任务上召回一次并共享结果，避免快速路径和正常路径重复召回。

完全匹配后构造 GoalSpec，更新会话为本轮已生成 goal 状态，保持用户输入/回答、task_result 的 m3 写入与原路径一致。执行结果仍经共享 format_result 和各渠道显示。

## 8. 存储与一致性

```text
~/.Karne/workflows/
├── index.sqlite
└── definitions/
    └── <workflow_id>/
        └── <version>.json
```

定义 JSON 使用以下字段，不把两套 graph DSL 混在一起：

```text
schema_version
workflow_id, version, created_at
name, purpose, applicability, exclusions
parameters, parameter_bindings
goal_template                  # GoalSpec 的受控模板，不是旧 goal 原样复制
graph_spec                     # 原生 GraphSpec JSON
required_capabilities          # 从图生成的精确版本清单
acceptance                     # 标准、证据要求、核验结果
source                         # request_id/run_id/graph_hash、提取模型和规则版本
```

索引包含主表 `(workflow_id, version)`、状态 candidate/enabled/paused、定义相对路径、定义内容哈希、可检索说明、创建/最近使用 UTC 时间，以及来源去重 `(run_id, graph_hash)`。使用记录按 `(request_id, run_id)` 去重，分别记录执行成功和业务核验结果，不把二者混为一个成功次数。FTS 仅对流程用途/适用范围建索引，不索引私人原文。

JSON 版本不可变；同一模板更新产生新版本。名称/ID 只能由模块生成，不能包含模型任意路径。目录 0700、文件 0600，默认不进入 Git。

一致性顺序：写临时 JSON→flush/fsync→原子排他链接发布完整文件→SQLite 事务发布定义哈希和状态。JSON 成功而索引失败只产生不可见孤立文件；读取仅通过索引，校验哈希和 schema，失败不执行。启动可清理临时文件，不能把未索引文件自动启用。SQLite 使用 WAL、短事务、busy_timeout；文件与索引同时遵守当前全局实例的互斥生命周期。

同一来源重复提交幂等。独立来源的近似模板先以结构与参数签名查重；相似说明不能自动覆盖已启用版本。初版保留独立候选而不引入复杂自动合并机制。

## 9. 失败、授权与回退

执行前失败（匹配、参数校验、缺工具、图校验）且确定没有业务节点启动时，可以在同一本轮进入一次正常理解/规划；总耗时和模型预算不能无限重新开始。

一旦任何业务节点启动，禁止自动将整个任务重新规划并从头重放。网络超时不能证明写文件、发消息等副作用没有发生；返回当前失败及部分产物，后续恢复需依据明确状态和授权。现有工具重试策略必须单独检查：对没有幂等保证的副作用工具，流程复用不能新增重试。

模板缺少当前授权时不扩大 policy。复用必须同时满足当前显式限制、宿主渠道已有授权、路径/对象范围；现有 ExecutionPolicy 只提供能力级授权，不等价于“覆盖任何文件”的授权。覆盖、发送等对象不明确时进入澄清；没有等待确认的执行机制时，在图启动之前处理，不能承诺运行中暂停。

工具版本不可用、模板损坏或定义不再兼容：不运行该模板。网络临时失败只记使用失败，不立即永久停用；结构不合法、来源否定或验收明确失败则暂停并记录原因。

## 10. 观测与验收

可观测覆盖整个生命周期，包含发现、提取、核验、保存和后续复用；不是仅在执行模板时记录命中结果。后台自动处理也必须能在现有观测页面独立检查。

### 10.1 后台链路与事件

| 阶段 | 事件 | 必须能回答的问题 |
| --- | --- | --- |
| 发现与提交 | workflow.discovery、workflow.submission | 哪次任务触发检查？是否进入队列？为何不值得提取，或为何被队列拒绝？ |
| 排队与运行 | workflow.job | 是否仍在等待、已经开始、结束、失败或被取消？排队和处理各耗时多少？ |
| 提取 | workflow.extraction | 提出了哪些稳定步骤和参数？复用价值与适用范围的判断依据是什么？ |
| 校验与核验 | workflow.validation | 来源哈希、参数回填、图结构和每项业务标准分别通过、失败还是 unknown？ |
| 去重与保存 | workflow.persistence | 是新版本、重复来源、保存失败还是已发布？定义文件和索引是否均已提交？ |
| 发布与暂停 | workflow.saved、workflow.paused | 当前状态是 candidate、enabled 还是 paused？原因和模板版本是什么？ |
| 匹配与执行 | workflow.resolve、workflow.parameter_binding、workflow.execution | 为什么命中或拒绝，参数来自哪里，是否跳过 planner，执行结果怎样？ |

耗时阶段使用现有 span 开始/结束机制，阶段结果区分成功、跳过、拒绝、失败和取消；不能把“不值得复用”显示成系统错误。原因采用稳定代码并提供简短说明，例如 not_reusable、source_unavailable、acceptance_unknown、duplicate_source、queue_full、persistence_failed；不只记录模型给出的自由文本。

发现的前台部分只记录来源及入队/拒绝，不增加模型调用或文件读取。后台 worker 在自己的 span 中记录价值判断和后续阶段，保持异步不阻塞契约。提取和核验模型使用现有 ObservedModel，保留输入、输出、调用用量、耗时和错误；不要求暴露模型未返回的内部思考过程。

### 10.2 跨前后台关联与状态

每个作业有 job_id 和独立后台 trace_id，显式携带 source_trace_id、source_request_id、source_run_id；产生模板后再附 workflow_id/version。后台不能隐式继承一个已经结束的前台 span，也不能为了提取作业把原任务一直显示为运行中。

任务详情关联展示“流程发现与固化”的后台作业，能跳转到作业时间线；流程/作业列表可按来源任务、作业状态及模板状态查询，展示队列等待、各阶段耗时、模型调用、判定依据和最终存储结果。用户应能区分“原任务成功，但后台提取失败”“候选已保存但未启用”与“已启用可复用”。相关页面内容和状态说明同步提供中英文。

正常退出先等待 workflow worker 完成并记录终态，再关闭 Observer，确保最后保存结果能落盘。强制退出后，若只发现开始事件而无结束事件，页面显示 interrupted/unknown，而不是永久运行或推断保存成功。观察事件缺失、脱敏或写入失败时显示记录覆盖不足，不以 trace 缺失判断模板不存在。

### 10.3 保存证据与隐私边界

workflow.saved 只能在定义文件和 SQLite 发布事务都完成之后发出，包含定义相对路径、内容哈希、版本及 candidate/enabled 状态；仅有原子文件写入不算模板发布成功。观察记录不参与执行授权，也不充当模板存储的权威数据源；展示当前状态时读取流程索引，事件时间线保留历史状态。

记录参数来源、schema 与必要的脱敏预览；来源正文、私人路径和模型载荷遵守现有敏感信息规则，不把密钥或完整私人文件复制到新的流程索引/事件摘要。观测失败不回滚已经正确发布的模板，也不能破坏原任务回答；必须通过现有观测健康/覆盖机制暴露记录失败。

流程提取、匹配与观测事件随对应功能同时落地，不作为功能完成后可选的补充工作。

至少验证以下可观察行为：

1. 完全匹配时 planner 调用数为 0，仍有正常编译/校验/执行记录；不是直接拿旧答案。
2. 同一模板跨天、跨时区生成正确本轮日期；不带来源输入、记忆或旧状态。
3. 参数缺失多轮补齐、用户改需求和取消均保持正确会话关联。
4. 目标相似但额外交付/副作用不同，不误命中；输入重述可以匹配。
5. 授权不足、工具版本变化、参数 schema 错、损坏模板均在任何业务节点前阻断。
6. 输出结构完整但业务不符时不启用；证据 unknown 的美观/渲染标准不冒充通过。
7. 队列满、后台模型失败、正常退出 drain、强退丢失符合契约，不影响当前答案。
8. JSON/SQLite 发布中断、来源缺失、重复提交、哈希不一致不产生可执行半成品。
9. 部分副作用后失败不自动从头重放；取消、超时和预算沿用执行库行为。
10. 新需求回退正常路径，已有 CLI、微信、m1/m2/m3 与观察记录契约保持。
11. 发现跳过、入队拒绝、提取失败、核验 unknown、去重、保存成功和保存失败均有可关联的观测结果；已完成前台任务不因后台作业变回运行中。
12. 保存事件与索引发布顺序一致；关闭顺序保证后台终态可见，强退未终结作业显示 interrupted/unknown；观测故障不破坏模板事务或用户回答。

验证以离线受控模型与工具为主，再在隔离目录做一次 Trending 等真实任务复用验收，分别报告首次规划与复用的调用数、耗时和业务结果；不依赖实时 GitHub 排名固定值。

## 11. 实施顺序与本阶段产出

1. 先落地 DynamicAgentGraph 的 run_graph 公共入口、GraphSpec 导出和无 planner/完整校验/状态隔离/取消回归测试。
2. Karen 落地模板契约、存储、后台生命周期与提取校验，并同时实现发现至保存的完整观测链路；无业务证明模板保持 candidate。
3. 接入匹配、参数补齐台账、本轮 GoalSpec 构造、run_graph 和正常路径回退，同时记录匹配、参数与执行观测。
4. 完成观测页面的关联展示与中英文说明，验证全生命周期记录，完成离线边界及隔离真实任务验收；README 中英文同步说明已实现行为。

实施已获用户授权，两个项目分别本地提交。首版不在指定图失败后自动重新规划，避免重置预算或重放副作用；真实验证使用隔离目录，不读取用户私人运行记录。其他实际接口差异以接口与运行文档为准。
