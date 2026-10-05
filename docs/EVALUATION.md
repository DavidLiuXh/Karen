# Karen 评测与修复协议 v1

本轮评测对象是 Karen 当前产品流程：输入路由、澄清、GoalSpec、记忆写入与召回，以及 DynamicAgentGraph 的实际规划与执行。采用真实 DeepSeek、Ollama BGE-M3，不以模拟模型结果冒充模型评测。

## 冻结标准

- CLAMBER：官方 `require_clarification` 是必要澄清的判定依据。0 时应直接回答或产生 GoalSpec；1 时必须澄清，而且提问应解决参考澄清指出的实质缺口。这里是意图评测，GoalSpec 不代表任务执行完成。源文件实际 3,202 条，每条双重 JSON 编码。只向被测模块提供 question/context，参考问题和预测字段不输入 Karen。
- LongMemEval：历史完整按时间排序输入现有 ContextMemory；保留 user/assistant 角色和会话边界；每条消息经实际提取、核验、向量化。完成 flush 后关闭再启动，证明持久化跨重启有效。答案和 has_answer/answer_session_ids 不进入记忆或意图模型。最终回答用独立评分调用核对参考答案，时间计算、知识更新、信息缺失必须准确。
- 中文回归：路由、跨轮澄清、日期、并存偏好、居住地变更、未知个人信息与取消任务。必须满足明确路由/状态/原子事实断言；结果不得暴露断言列出的内部元数据。未知信息和解释类问题另作语义评分。未来扩充用例须保留本轮用例并另开基线。
- 执行成功必须是 COMPLETED 且 output_complete=true，同时通过业务断言。时间只存在 GoalSpec.context 不算日期约束正确，必须出现在目标、输入或成功标准中。
- 评分模型为 DeepSeek 的单独调用，使用固定评分提示词；不是官方 LongMemEval GPT-4o 分数。评分失败记 judge_error，不改判成功；硬断言失败不能被评分模型推翻。
- 每例首次尝试为本轮结果。既有模块重试保留在 model_calls 和 trace；不可选择某次成功覆盖前一次失败。修复后新建运行目录、保留旧结果。错误和判定失败均计入总分母。

## 数据范围与局限

CLAMBER 每个 category × label 各取两例，按原始行号确定开发/留出：共 12 例。LongMemEval oracle 每种 question_type 加 abstention 取两例，按完整历史消息数、ID 排序：共 14 例。中文合成用例共 12 例，8 个开发、4 个留出。先跑开发集取得基线并修复，然后首次打开留出集验收。

Oracle 只含官方证据会话，是短历史诊断，不能报告成完整 LongMemEval_S/M 的抗干扰召回能力。适配器也接受 S/M 的相同字段，但本轮不声称已经运行 S/M。优先短历史的抽样不具总体代表性，报告必须给出样本数和类别。公开数据时间不含时区，固定解释为 UTC，不能假称知道原作者时区。

数据预检发现 oracle 500 条中 43 条含晚于 question_date 的历史会话。协议拒绝未来泄漏；将这些原始记录作为无效输入单独列入 manifest.excluded_invalid_records，之后在有效记录中抽样。不会删除某例未来消息后继续把它计作成功，也不会因模型失败改变排除标准。

所有记忆、m3、SQLite、文件工具、执行记录、可观测记录在各例独立 runs/evaluation 目录。不会读取 ~/.Karne/context 或用户实际历史。中文天气执行使用符合正式工具 schema 的离线固定搜索响应；测试日期解析和规划，不能证明真实天气数据正确。文件读写使用真实工具但范围限制在例内 artifacts。没有注册浏览器或发送消息工具。

## 运行

公开源：

- https://github.com/zt991211/CLAMBER （clamber_benchmark.jsonl）
- https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned （longmemeval_oracle.json）

原始文件放在 runs/evaluation/data（不提交大数据），tests/fixtures/evaluation_selection_v1.json 保存校验和和固定 ID。全量 manifest 和每次 run.json 保存输入、版本、源码哈希，不能覆盖已有运行目录。

```bash
uv run --env-file .env python -m karen.evaluation prepare \
  --data-dir runs/evaluation/data --output runs/evaluation/manifest-v1.json
uv run --env-file .env python -m karen.evaluation run \
  --manifest runs/evaluation/manifest-v1.json \
  --split development --output runs/evaluation/baseline-v1 --concurrency 1
```

run 支持 --suite、--case 精确选择，--split heldout 验收留出集。每例 result.json 和汇总 results.jsonl/summary.json 包含首次结果、失败阶段、判定、模型调用/耗时、实际使用量；observability 下保存因果追踪。诊断时优先定位路由、澄清、提取、召回、评分、规划、执行哪一步失败，不直接修改最终答案。

建议串行运行真实模型评测（`--concurrency 1`），减少服务并发与本地资源竞争的干扰。运行开始和结束分别校验两个项目 src 下的 Python、提示词文本和 JSON 资源哈希。`run.json` 的 `source_integrity=stable` 才能用于同版本比较；源码变化时保留所有结果并以 `SOURCE_CHANGED_DURING_EVALUATION` 报错，不能把该轮当作冻结版本验收。旧轮次没有结束校验，需在报告中明确其验证范围。

产品召回采用“先融合、再精排”：粗召回最多保留 80 条候选及其版本关系，不因完整记录的字节大小提前丢弃。精排使用语义摘要、来源角色、时间与状态等元数据，整个模型请求限制 24KB；失败时沿用融合顺序并标记降级。最终上下文单独限制 12KB；m3 扩展限定在至多三个召回任务内，保留相邻用户补充和必要的助手衔接。展示时省略重复引文、截断的助手衔接有明确标记；完整原始记录留在 m3。

独立变体保存在 `tests/fixtures/evaluation_variants_v1.json`；转换成相同 manifest 格式后单独运行。变体明确关闭中文天气固定搜索响应，避免把与问题无关的固定资料提供给执行器。变体用于修复后补充回归，一旦用于诊断和修复，就不能继续声称它们是未见数据。

## 修复门槛

先保存完整开发基线，再按机制归类问题。通用修复不得读取评测 ID、参考答案、题面关键词白名单，或为某例加特殊分支。修复需有验证可观察行为与边界的离线测试，并对相应开发例真实复测；每个问题在对应项目单独本地提交。留出首次验收结果完整保留；若需据此继续修复，它就变成开发证据，不能再称未见留出。必要时补独立变体进行后续验收。
