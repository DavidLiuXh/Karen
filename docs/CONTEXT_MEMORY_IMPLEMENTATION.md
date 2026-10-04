# 上下文记忆：运行与验收说明

日期：2026-10-03。按最终详细设计实现，默认接入 CLI。所有代码位于 Karen；没有改动 DynamicAgentGraph 源码。

## 运行

```bash
uv sync
ollama serve                     # 已运行时无需重复启动
ollama pull bge-m3:latest         # 已安装时无需重复下载
uv run --env-file .env karen --timezone Asia/Shanghai
```

`.env` 提供已有的 `DEEPSEEK_API_KEY`，可选 `TAVILY_API_KEY`。默认记忆目录是用户指定拼写的
`~/.Karne/context`。只有 `start()` 创建目录；测试和下面的性能脚本使用临时目录。
Ollama 暂不可用时，已有记录可降级为 BM25；派生向量任务会报告失败并保留原文和已提交事实。
目录锁冲突、存储初始化失败会明确结束启动，避免在用户不知情时停用记忆或启动第二个写入进程。

## 实现与公共接口

| 文件 | 职责 |
| --- | --- |
| `src/karen/context/contracts.py` | 事件、receipt、状态、事实/版本、查询与返回结果的 Pydantic 契约 |
| `src/karen/context/service.py` | 队列、两个后台 worker、前台优先、生命周期与原文检索 |
| `src/karen/context/storage.py` | JSONL/附件持久化、SQLite 原子修订、FTS5、向量存储及恢复 |
| `src/karen/context/extraction.py` | LangGraph：extract → verify → commit；来源核对及事实变化 |
| `src/karen/context/retrieval.py` | LangGraph：analyze → retrieve → rank → assemble；装配阶段按需补查原文 |
| `src/karen/context/prompts.py` | 独立的提取、核验、查询理解与精排指令 |

`ContextMemory(root_dir=..., model=..., embeddings=...)` 使用现有通用 `ModelClient` 与 LangChain
`Embeddings`。LLM 无提供商专用逻辑；Ollama embedding 在装配边界创建，核对已安装模型摘要以识别 latest 标签变化。
其他 Embeddings 实现须提供稳定的 `model` 标识；不同模型标识、摘要或向量维度不会混用。

| 调用 | 行为 |
| --- | --- |
| `await start()` | 获取单写入目录锁，初始化/检查存储，恢复未登记文件尾部与任务 |
| `submit(ContextEvent)` | 校验、脱敏并隔离输入，立即返回 WriteReceipt；不等待磁盘或模型 |
| `async with foreground()` | 暂停发起新后台模型/embedding 调用，原文落盘继续；支持嵌套 |
| `await recall(RecallQuery)` | 本轮等待最终召回结果；自动进入 foreground；总期限 20 秒 |
| `await search_details(DetailQuery)` | 直接读来源或按明确任务/会话/日期范围查 JSON 字段；无范围返回 needs_scope |
| `await write_status(receipt)` | 分别报告 raw、derived、index 状态和安全错误码 |
| `await flush(receipt=None)` | 等待指定事件或调用时水位内事件的派生与索引终态；失败返回汇总异常 |
| `await reindex()` | 重建 BM25 并使向量后台重建，保留原有事实、来源及修订 |
| `await close()` | 停止接收，等待原文、附件与恢复状态持久化，取消未完成模型请求并释放资源 |

`flush/close` 在调用方前台区间结束后调用，避免等待被自己暂停的后台步骤。
取消召回会传播取消，不转换为成功降级。终端输入使用可取消等待的独立读取线程，避免默认 executor 等待 stdin 导致 Ctrl+C 退出挂起；真实终端已验证退出码 130。关闭等待已经发出的磁盘线程完成；取消模型请求不保证服务端停止计算。
有限重试耗尽的派生任务在下次启动重新尝试；已提交事实只重试索引。重试不会重复应用事实替代。

## 数据与一致性

- m3 按事件的用户当地日期写 JSONL；每行有 schema、提交序号、事件快照及校验摘要。
  超过 16 KiB 的字符串写成内容寻址附件，原始内容在读取时恢复。
- 原文和附件 fsync 后登记事件及 write_jobs，才报告 persisted。正常退出无额外确认，也不设置丢弃原文的关闭超时。
- fsync 后登记前中断时，下一次启动依据文件水位恢复。只截断不完整的最后一行；完整坏记录报告损坏。
- 来源角色由捕获事件决定，LLM 只能引用真实 event_id、JSON pointer 和连续原文。助手复述不能新增用户 m1 支持来源。
  task_result 是程序捕获结果；其输出仍可能由 LLM 生成，不能视作所有内容均被工具独立验证。
- m1 的替代、纠正、冲突以及关联 m2 在同一事务提交。补充支持来源保留最早记录时间；未知变化时间保留 null。
  支持来源时间、现实有效日期与修订提交时间分别存储；day/month 精度不变成虚构的精确瞬间。
- known_at 根据已提交账本重建事实，不引用未来提交的版本。当前问题可从旧名称定位当前版本；过期、纠正、冲突及不明时间口径的事实标作 evidence_only。
- 提取核验不确定的候选只作为带 uncertain 标记的 m2；不进入可用于个性化的 m1。
- SQLite 使用一个 memories 表和内嵌来源 JSON 表达 m1/m2，不另建重复来源表。向量模型、维度、文本摘要和 indexed_at 在 embeddings 中；任务状态在 write_jobs 中。
- 显式凭据在提交边界脱敏，事件 redacted_fields 标注位置。个人信息仍作为用户要求的记忆保存。

日常是最终一致读取：原文可已落盘而派生尚未完成。`coverage.index` 报告派生和索引的积压/失败；
`snapshot_revision` 描述本次提交记忆的水位，相关原文的 storage_state 独立标明。需要严格 read-after-write 时使用 flush。

## 召回、关联与时间线

每层取向量与 BM25 各 Top-100，RRF 先融合，再选每层最多 20 个主候选，最多 80 条关联记录；
整个精排数据输入含定位线索最多 24 KiB，最终上下文最多 12 KiB。默认最终选 m1 五组、m2 八组及四个详情片段。

查询的时间口径以 QueryAnalysis 契约为准：effective_at/known_at 必须提供带时区的 at；
time_range 筛选原始记录发生时间，不能代替 at，也不是新任务的目标日期。
例如“查明天的天气”不限制记忆必须发生在明天；“昨天我问过哪些问题”才按用户时区筛选昨天的事件。
不完整的模型响应保留降级，不从区间起点猜造精确时点。

精排同时接收当前任务 ID 和当前任务澄清。候选需补充当前请求所需信息；相同话题、
重复提问或已被当前输入覆盖的旧澄清不构成相关性。回顾澄清或延续需要旧约定的任务仍可召回它们，
不按事件类型一概删除。提示词判断仍需根据实际使用持续校准。

先分析当前输入与当前任务澄清，不输入旧任务对话。QueryAnalysis.dialogue_dependency 区分独立输入 none、
已有澄清链可解释的 current_task，以及需要外部历史的 needed/uncertain。current_task 仍可召回相关事实，
但不读取 history_candidates，也不要求从 m3 再确认当前会话；当前任务自己的 m2 摘要不重复召回。
外部历史候选排除当前 request_id，避免把澄清链当成另一段待定位历史。
只有需要或可能需要外部历史时，读取近期三个任务、十二个原始事件，
以及粗检索定位到的有限旧来源；精排阶段确认真实任务/事件 ID。确定相关才输出 history.messages。
外部历史歧义或必要历史不可用时，意图模块澄清，不启动业务执行。下一次回答重新分析并召回，随后继续意图判断。
已有当前澄清链直接传入意图模块，相关事实召回或精排降级不阻止它继续评估原请求。

当前属性只补齐命中版本、当前版本和完整冲突组，不把百次历史变化都带入一个普通问题。
时间线查询可以展开更多版本，受同样预算限制；coverage.relation_mode 与降级标记说明裁剪，不能声称已得到完整历史。
候选包按组裁剪，不保留缺少当前替代版本的旧命中。精排失败保持融合位置顺序，并标 unverified；缺乏可解释关联时允许降级空上下文。

m3 按已登记偏移读取并验证内容，再查解码后的字段/附件，不调用 grep，也不接受模型指定任意文件路径。
一次查找最多 200 条事件、2 MiB、2 秒，达到限制报告 partial。缺原文不能虚构引用。

collection 查询由参数化 SQL 按日期与执行状态统计已记录 RunResult，request_id 去重，最多展示 50 项；
总数不受 m2 Top-K 限制。时间范围缺失进入澄清，仍在原文队列中的结果标明统计未追平。
COMPLETED/output_complete 是引擎结构状态，不代替用户业务验收。

## Karen 接入

CLI 默认创建并启动记忆，将它注入 Karen。一次输入先分类并选择请求身份，再非阻塞 submit 原文；问题和任务 recall → 意图判断/澄清 →
GoalSpec.context.memory → 引擎执行 → task_result 记录 → 实际显示回复记录。新任务更新 request_id，conversation_id 保持。
当前明确要求覆盖旧偏好；当前项目的偏好优先于全局默认，项目身份只取调用方显式 project_id。

独立 API 调用方在 UI 展示结果后调用 `agent.record_response(session, displayed_text)`；不要用另一份摘要代替真正显示的回复。
只调用 IntentRecognizer，或构造 Karen 时不注入 memory，保持现有无记忆使用方式。记忆信息不能增加 ExecutionPolicy 权限。

## 验收证据与边界

离线检查：94 项 pytest 测试通过，`ruff check .` 与格式检查通过。测试不调用真实 API，不触碰全局用户数据。
后续日期、中文终端输入、退出清理与召回质量修正后的完整回归为 140 项通过。真实 DeepSeek 合成验证覆盖
新任务目标日期、历史事件范围、effective_at/known_at 时点，以及旧澄清排除和按需回顾关联。
复验命令：`uv run pytest`、`uv run ruff check .`、`uv run ruff format --check src tests scripts`。
覆盖多轮澄清与真实引擎离线执行、来源/作用域、时序和冲突、正常退出/重启恢复、坏记录、队列与磁盘失败、
向量/BM25/精排降级、索引重建、无关请求隔离、关联原文与歧义澄清、详情范围及集合统计。

2026-10-03 使用现有 DeepSeek 与本机 BGE-M3，在临时目录执行合成数据测试：

| 场景 | 实际结果 |
| --- | --- |
| 提取、核验、索引、召回、精排 | 五个步骤成功，没有降级 |
| 北京搬到上海后问当前城市 | 上海 active；北京 superseded 且 evidence_only；无无关对话历史 |
| 太阳温度这个独立问题 | empty，m1/m2/history 均为空 |
| 修改刚才的项目日报页面 | selected，定位到实际原请求 |
| 完整 Karen + DynamicAgentGraph 链路 | COMPLETED、output_complete=true、有 answer、GoalSpec 包含 memory；关闭后四类原始事件均存在 |

后三个召回测试约 3.4–4.2 秒；完整执行合成任务约 11.85 秒。这是少量接口与流程验收，不是通用语义准确率或 P95 保证。

性能脚本可重复运行：

```bash
uv run python scripts/benchmark_context.py --records 10000
```

本机 Intel i7/16 GiB 下，10,000 条合成摘要、1024 维固定向量，快照读取 + BM25 + 余弦 + RRF
约 0.63–0.73 秒，文件总量约 65.8 MiB；该次批量元数据/FTS 写入约 14.2 秒。
脚本排除 LLM 和 embedding 推理，不把 SQLite 理论容量视为检索性能保证。
第一版精确扫描随记录数增长；更大规模、长关系链及实际输入的抽取/关联质量仍需持续测量。

当前不实现主动发现任务、记忆删除/归档、ANN 或模型选择平台，保持已确认的模块范围。


## 个人事实查询与回答展示

查询理解的 `facts` 类型用于个人长期事实、偏好及其列表；`collection` 专用于历史任务/对话枚举。
默认当前事实查询不索取历史起止日期。当前事实召回优先精排 m1 的候选和必要版本/冲突关系，
无 m1 候选时才使用 m2 线索。历史事实口径仍保留两层候选。事实列表允许最多 20 个 m1 主候选，
仍受既有字节预算约束；截断会记录降级，不能声称已穷尽用户全部事实。

回答策略同时供意图识别和 GoalSpec 使用，避免把内部证据、记录时间、来源 ID 和预算状态
添加为用户未要求的成功标准。CLI 有 answer 时不重复附上标准 evidence/limitations；
必要缺失、冲突、失败和用户要求的来源需自然写入 answer。无 answer 的回退展示与结构化输出保留。
原始模型输出和召回元信息继续进入观测记录，`--json` 仍输出完整 RunResult。

回归包含：两次偏好写入后重启再查询、无关的大量 m2 摘要、六项爱好、当前城市版本与冲突，
以及日常展示和审计输出隔离。另以合成数据验证真实 DeepSeek 的查询分类及完整执行链路。


输入分类后的纯信息告知与普通交流可以直接回应，生成的用户原文及实际回复继续进入 m3，
不生成 goal_created/task_result；问题直接回应前仍召回相关记忆。后台记忆写入与业务执行状态独立。
路由与相关测试见 [输入路由说明](INPUT_ROUTING.md)。
