# 可复用流程：接口与运行

## 使用方式

更新 Karen 与配套 DynamicAgentGraph 后执行 `uv sync`，继续使用原启动命令：

```bash
uv run --env-file .env karen --timezone Asia/Shanghai --observe
```

不需要额外开关。新任务执行完成后，后台 worker 从实际运行记录提取模板。首次成功可以自动启用，但必须通过结构校验和有证据的业务核验；candidate 不参与自动执行。当前不扫描历史 runs，也不重放工具来“试验”模板。

下一次请求仍先输入路由、召回相关约束；匹配已启用模板时直接生成本轮 GoalSpec 与执行图，省去完整清晰度/意图提取和 planner。缺少必需参数进入同一请求的补参循环；目标、范围、交付或副作用不适用时走正常理解路径。已有普通澄清不被流程匹配抢占。

日期默认值仅支持提取时明确声明的“本轮用户当地今天”；其他参数不把来源值当默认值。已绑定参数在补参期间保留，日期参照首次输入时间和用户时区。节点指令中写死变化值、未参数化输入或缺少核验证据的模板不会自动启用。

## 模块接口

独立包 `karen.workflow` 导出：

- `ReusableWorkflows(root_dir, runs_dir, model, observer)`：注入现有 ModelClient 和 Observer。
- `await start()` / `await close()`：存储锁、单 worker、退出 drain；先关闭流程 worker，再关闭 Observer。
- `submit(CompletedWorkflowRun)`：非等待式入队，返回 accepted/job_id 或拒绝原因；不在前台读文件或调用模型。
- `await resolve(WorkflowRequest)`：同步等待完整匹配/绑定结果；15 秒总预算，不确定或失败返回 no_match。
- `record_outcome(...)`：异步核验复用结果，统计每次运行；明确业务失败或图失效暂停模板，不自动重放。
- `review_feedback(...)`：仅关联刚才使用的流程，后台判断明确否定结果的反馈；必须引用当前用户输入中的原话，独立新话题不应暂停流程。

CLI 装配默认 DeepSeek 非思考模型用于发现、核验、匹配与反馈，传输由现有客户端管理。worker 模型继续由执行库装配。流程模块自身的每次模型调用有 60 秒时限，结构错误或截断最多恢复一次，截断按通用规则增大输出预算；后台每作业总时限 120 秒，因此不会无限重试。输入载荷上限 256 KiB。单队列最多 32 个作业，队列满拒绝额外工作，不阻塞交互。模板候选检索使用 SQLite FTS5，最多 5 个候选进入匹配。Karen 的执行节点上限为 180 秒、整次引擎运行仍为 600 秒；流程模块的匹配与后台调用时限不随之增加。

`WorkflowResolution.status` 为 matched / needs_parameters / no_match。matched 包含本轮独立的 goal、graph_spec、模板 ID/版本及已绑定参数；needs_parameters 包含问题与已解析参数。内部原因不自动附加到用户回答中。

## 执行库协议

配套 DynamicAgentGraph 提供公共 `GraphSpec` 以及：

```python
result = await engine.run_graph(
    goal=current_goal,
    graph_spec=current_graph,
    policy=current_policy,
    cancellation_token=current_token,
)
```

该入口不调用 planner，沿用现有校验、编译、预算、取消、输出和运行记录。每次运行拥有新 run_id 和新状态。模型生成内容的节点仍调用 worker。当前实现不在指定图失败后自动另起规划运行，以免重置预算或重复副作用；无效模板记录失败并在后台暂停，后续请求可走正常路径。

模板只保存原生 GraphSpec。参数变化通过 input_field 映射和精确 binding site 将 literal 提升为 input 引用，不能任意修改图、拓扑、schema 或 instruction。参数 schema 直接使用执行库 SchemaSpec，并检查与来源输入/绑定目标的兼容性。新 GoalSpec 不复制旧 context、conversation、memory 或授权。

参数化核验会同时获得来源图及实际参数化后的 prepared_graph/prepared_goal，依据后者核对引用和输入 schema；不能因来源图中仍有已提升的 literal 而误判绑定失败。没有可用的已启用模板时，workflow.resolve 会记录拒绝原因与各状态模板数量。

启用依据不是模型一句“成功”：核验须覆盖原 GoalSpec 的全部成功标准，并提供以 `/evidence/` 开头、实际存在的执行证据指针。主观质量、外部浏览器渲染等没有独立证据时仍为 unknown。业务判断依然包含模型评估，不能保证所有语义错误都被发现。

## 本地存储与失败边界

```text
~/.Karne/workflows/
├── .lock
├── index.sqlite
└── definitions/<workflow_id>/<version>.json
```

JSON 定义不可变，索引是模板状态的权威来源。保存先写临时文件并 fsync，以原子排他链接发布完整文件，再提交 SQLite 元数据/FTS 事务；不会覆盖已有版本。未索引孤立文件不会加载或启用，读取时检查路径、哈希和定义版本。来源使用配置 runs_dir 下的 run_id 和 graph_hash，不能通过任意 graph_ref 读取其他文件。目录 0700、文件 0600，不进入 Git。

正常退出等待队列处理及写入完成；强退可以丢失尚未入库的候选/统计。存储损坏或锁被占用时，CLI 记录 workflow.unavailable，保持普通交互。观察故障不影响模板事务；模板事务失败不影响原任务结果。

## 可观测性

`--observe` 的“可复用流程 / Reusable workflows”面板展示当前模板状态和后台作业，可按模板/作业字段搜索，跳转后台详情或来源任务。模型输入/输出、发现、提取、核验、保存的全过程独立记录，敏感数据沿用现有脱敏规则。

- 来源任务完成后保持完成状态，不因为后台提取而重新显示运行中。
- 每个作业有独立 trace_id/job_id，以 source_trace_id 和 request_id 关联来源。
- workflow.discovery/submission：是否值得检查、入队或拒绝。
- workflow.extraction/validation：参数、适用范围、校验和业务证据。
- workflow.persistence/saved：去重、文件与索引发布结果；saved 在事务提交后记录。
- workflow.resolve/parameter_binding/execution：匹配、参数来源与实际执行。
- workflow.model_retry/paused/unavailable：协议恢复、暂停与存储不可用。
- 未记录终态且进程已结束的排队/运行作业显示 interrupted_unknown，而非保存成功。

界面支持中文和英文；原始参数、模型结果和技术标识保留原值。列表受现有 trace 文件/字节预算及模板条数限制，覆盖不足时显示警告；事件缺失不能证明步骤没有发生。

## 验证

离线测试：

```bash
uv run pytest tests/test_workflow.py tests/test_observability.py tests/test_cli.py
```

覆盖真实引擎运行记录→后台提取→重启加载→跨日参数绑定→指定图执行，以及证据不足、错误 schema、缺参多轮、取消、权限、工具版本、模板损坏、发布失败、队列饱和、退出等待、去重、反馈暂停与副作用失败不重放。

2026-10-24 隔离真实模型验收：实际抓取 GitHub Trending daily，模型提取项目/链接、生成 HTML，并通过文件工具写入和回读。后台自动提取并启用模板；新请求改为 weekly 与新文件路径，匹配后完成同样处理，planner 调用数 0，后台复用验收 supported。来源图由受控 fixture 提供，首次不是实时模型规划；此验收验证流程提取和复用链路，不是自主规划质量或全部项目覆盖率的评测，也未验证原生浏览器渲染、完整 HTML 文档结构或页面美观。
