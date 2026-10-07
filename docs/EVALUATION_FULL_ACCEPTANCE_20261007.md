# Karen 固定评测清单完整复测（2026-10-07）

产品版本 `6a0f483`。固定 66 条结果 **54/66（81.8%）**，上一轮为 **47/66（71.2%）**，净变化 **+7 条**。

| 测试集 | 上一轮 | 本轮 | 变化 |
| --- | ---: | ---: | ---: |
| CLAMBER | 13/24 | 14/24 | +1 |
| LongMemEval oracle | 9/14 | 13/14 | +4 |
| Karen 中文回归 | 12/12 | 11/12 | -1 |
| 独立变体 | 11/12 | 12/12 | +1 |
| 四条边界回归 | 2/4 | 4/4 | +2 |
| 合计 | 47/66 | 54/66 | +7 |

新增八条澄清回归单列为 **8/8**，不加入原 66 条分母。离线测试 **356 项通过**，Ruff 通过。

这里的全量指当前固定清单，不是公开 CLAMBER / LongMemEval 数据集全集；LongMemEval 使用 oracle 历史和自定义 DeepSeek 评分，不是官方榜单成绩。

## 结果说明

整体分数提高，但还不能称为稳定达标。本轮有 9 条由未通过转为通过，同时有 2 条由通过转为未通过。当前最明显的改善在 LongMemEval（9/14 → 13/14）；中文交互反而从 12/12 降至 11/12。

- 中文多轮澄清仍不稳定：用户已说明“收件人是王经理，中文，设计已完成，下周开始开发，直接给我邮件草稿即可”，Karen 仍要求项目名称，导致无法交付草稿。这一项上一轮通过、本轮失败。它也说明过度澄清并非只发生在英文输入。
- 常识分类新增过度澄清：clamber-1658 询问温度和压强，而冻结预期是直接进行物态分类。
- 复杂资料推荐仍无法完成：75832dbd 本轮在 planning 阶段报告 MODEL_RESPONSE_TRUNCATED；上一轮是执行节点时限失败。本次没有调整输出上限或预算。
- CLAMBER 的主要剩余差异仍是是否需要澄清：包括公开信息先查证、作品多版本处理、代词解析等。先按冻结判定保留失败，不能为了提高分数直接改变产品策略或标签。
- clamber-2050 本轮已经返回澄清，符合该例的硬性 outcome=clarification；评分模型却以没有输出 X/Y 为由判失败，与本例预期澄清的要求不一致。原 failed 不修改，单独注明评分分歧。

新增八条本轮全部通过，但相同最终产品代码此前独立重复曾为 7/8。因此 8/8 是本次结果，不能抹去此前波动，也不能证明所有封闭条件判断已经可靠。

本轮仍使用原提示词，没有做中英文提示词对照实验，不能用本轮结果判断是否需要双语版本。

## 可比性

与上轮使用相同清单哈希、协议 1、参考答案、并发 2、DeepSeek 低强度思考（记忆非思考）、embedding、评分模型及执行库源码。两次新运行的产品/执行库源码完整性均 stable。没有修改产品代码、预算、标签或评分标准，没有补测覆盖失败。

API 服务状态、模型随机性和实时检索结果仍会变化，因此一次分数变化是观测结果，不能单独证明代码改动的因果效果。格式错误、超时、评分接口失败均计入原分母。

## 前后变化的用例

由未通过转为通过（9 条）：clamber-0310、clamber-2813、6456829e_abs、09ba9854、caf03d32、a82c026e、expansion-no-shared-condition、boundary-shared-string-rule、boundary-relative-listening-preference。

由通过转为未通过（2 条）：clamber-1658、zh-clarification。

## 本轮全部未通过用例

| 用例 | 测试集 | 本轮状态 | 上一轮状态 | 原始诊断/判定 |
| --- | --- | --- | --- | --- |
| clamber-0191 | CLAMBER | failed | failed | outcome: expected clarification, got goal |
| clamber-0278 | CLAMBER | failed | failed | outcome: expected clarification, got goal |
| clamber-2050 | CLAMBER | failed | error | The candidate does not answer the question. The reference asks whether the given sentence's category is either "does not contain a negation" or "proper noun" (i.e., whether to output X or Y). Instead, the candidate challenges the premise and asks for clarification, so it fails to provide the required X/Y determination. |
| clamber-2759 | CLAMBER | failed | failed | outcome: expected complete, got clarification |
| clamber-2664 | CLAMBER | failed | failed | outcome: expected complete, got clarification |
| clamber-2825 | CLAMBER | failed | failed | outcome: expected clarification, got goal |
| clamber-1658 | CLAMBER | failed | passed | outcome: expected complete, got clarification |
| clamber-1174 | CLAMBER | failed | failed | outcome: expected clarification, got reply |
| clamber-0997 | CLAMBER | failed | failed | outcome: expected clarification, got goal |
| clamber-0730 | CLAMBER | failed | failed | outcome: expected clarification, got reply |
| 75832dbd | LongMemEval oracle | failed | failed | execution did not complete |
| zh-clarification | Karen 中文回归 | failed | passed | outcome: expected execution, got clarification; answer missing: 王; answer missing: 设计 |

## 新增澄清回归未通过用例

无。

## 原始结果

- 上一轮：`runs/evaluation/structural-acceptance-v6`
- 本轮完整 66 条：`runs/evaluation/full-acceptance-20261007-v1`
- 新增八条：`runs/evaluation/full-clarification-20261007-v1`
- 各目录的 run.json 保存源码哈希与配置；results.jsonl 保存全部原判定；用例目录保留记忆、执行及观测追踪。
- 本轮 comparison.json 保存分组、全部失败和逐例前后变化。
