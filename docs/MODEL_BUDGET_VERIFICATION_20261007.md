# 思考配置、耗时与截断恢复验收（2026-10-07）

已落实本轮配置调整，首次输出预算不变。没有修改评分协议、公开标签或判定标准，
没有增加特定评测场景的产品规则。本轮只抽查三条既有案例，不能视为固定 66 条的全量重跑或达标率提升证据。

## 默认配置

| 阶段 | 思考配置 | 首次输出 tokens | 时间边界 |
| --- | --- | ---: | --- |
| 输入路由、意图及其复核、直接回应及其复核 | low | 16384 | 既有 60 秒阶段期限 |
| 清晰度及其复核 | low | 8192 | 既有 60 秒阶段期限 |
| DynamicAgentGraph 规划 | medium | 16384 | 既有单次 60 秒、执行总期限 600 秒 |
| DynamicAgentGraph 执行节点 | low | 16384 | 既有 60 秒节点期限 |
| 记忆提取、核验 | 非思考 | 8192 | 既有 30 秒阶段期限 |
| 查询理解 | 非思考 | 4096 | 8 秒阶段期限 |
| 精排 | low | 4096 | 20 秒阶段期限（原为 8 秒） |

同步召回总期限从 20 秒调整为 45 秒。各阶段重试共享阶段剩余时间和召回总期限，
装配保留原有的 1.5 秒余量。CLI 与非意图专用评测的模型装配一致；CLAMBER 仍只调用意图流程。

DeepSeek [官方接口文档](https://api-docs.deepseek.com/api/create-chat-completion/)目前接受兼容值
`medium`，但映射到 `high`。请求按用户要求传 `medium`；元信息分别记录
`reasoning_effort=medium` 与 `effective_reasoning_effort=high`。这是配置说明，不能把它解释为提供商拥有独立的中档。

`ContextMemory` 可显式注入 `rerank_model`；未注入时仍使用调用方给出的 `model`。
CLI 把 low 思考客户端注入精排，把非思考客户端注入提取、核验和查询理解。
所有模型仍遵守原有 `ModelClient` 接口。

## 截断恢复

`MODEL_RESPONSE_TRUNCATED` 允许在原有尝试次数内翻倍一次输出预算：4096 → 8192、8192 → 16384、
16384 → 32768。自动恢复最高 32768，同时尊重客户端更低的显式硬上限。
传输适配器的硬上限提高到 32768，以允许恢复请求；`ModelRequest` 和 `EngineConfig` 的首次默认值仍为 16384。
后续独立请求不会继承上次扩大的预算，也不修改共享客户端状态。

输入/意图和记忆提取沿用已有的一次内容修复，不叠加另一套重试循环。
查询理解和精排增加一次截断专用恢复；规划和执行沿用原有规划轮次、节点尝试次数和调用预算。
普通格式/schema 错误不增加输出预算。后台记忆内容恢复耗尽或无法扩预算时，不把它当作临时故障重复执行整份作业。

恢复重新生成完整响应，保持原输入、输出 schema 及必要证据核验，不接受或拼接半份 JSON。
工具没有新增重试。取消立即向上传播，不重新启动已执行的工具。
连续截断明确失败；精排失败沿用已有融合顺序并标记 `RERANK_FAILED_FUSION_ORDER` 和 `unverified`。

Karen 记录 `model.output_budget_expanded` 及每次请求的 `max_output_tokens`、剩余时间和模型配置。
DynamicAgentGraph 记录 `model_output_budget_expanded`，事件通过 `payload_ref` 指向预算变化记录。

## 验证

- Karen：全套回归 **363 passed**；随后新增的客户端硬上限/后台作业边界测试 **1 passed**，合计 364 项。
- DynamicAgentGraph：本次修复工作树全套 **331 passed, 10 skipped**；跳过项需要外部运行条件。
- 两个项目 Ruff 和 `git diff --check` 通过。
- HTTP 模拟测试确认请求实际发送的 `max_tokens` 为 16384 → 32768，后续独立请求恢复为 16384；同时核对 low/medium 的真实请求字段。
- 行为测试覆盖规划与节点恢复、连续截断、显式传输上限、调用/尝试预算、记忆作业不重复提交、
  查询与精排模型分离、原始输入/schema 保持、阶段截止时间、取消以及融合降级。

真实 DeepSeek + 本机 BGE-M3 的独立抽查目录：
`runs/evaluation/model-profiles-output-recovery-v1`，来源为已有的 `manifest-structural-repair-v1.json`，
冻结判定不变，source_integrity 为 stable。

| 案例 | 结果 | 观测证据 |
| --- | --- | --- |
| zh-hobbies | passed | 查询理解非思考/8 秒；精排 low/20 秒，实际约 3.1 秒 |
| zh-residence | passed | 当前住所召回；精排 low/20 秒，实际约 15.4 秒 |
| zh-relative-date | passed | 规划 medium（有效 high）/16384，实际约 35.8 秒；执行节点 low/16384 |

三条没有自然触发截断。扩预算的 HTTP 参数和恢复边界由模拟响应验证，没有为了演示主动消耗数万 tokens 制造截断。
真实运行通过不证明澄清语义、记忆覆盖率或完整评测集的问题均已解决。

代码本地提交：Karen `38ff7e8`；DynamicAgentGraph `cbf10b6`（修复工作树
`/private/tmp/dynamic-graph-reducer-search-results`）。DynamicAgentGraph 四个运行时文件的本次补丁
已同步到 Karen 实际引用的 `/Users/lw/opensource/DynamicAgentGraph`，未提交或覆盖该目录已有的其他修改。
