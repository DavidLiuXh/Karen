# 最新固定评测清单重跑报告（2026-10-10）

**最新整体版本没有取得综合提升。** 有效完整轮为 **49/66（74.2%）**，10 月 7 日历史完整轮为 **54/66（81.8%）**，净减少 5 条、下降约 7.6 个百分点。两条改善、七条退步。

本次仅评测和诊断，没有修改产品代码、提示词、判定标准或失败用例。完整轮 2026-10-09 22:37–23:06（Asia/Shanghai）运行，目录名称沿用启动日期。

## 同一固定清单、协议 1 的结果

| 测试集 | 历史完整轮 | 最新完整轮 | 变化 |
| --- | ---: | ---: | ---: |
| CLAMBER | 14/24 | 12/24 | -2 |
| LongMemEval oracle | 13/14 | 10/14 | -3 |
| Karen 中文回归 | 11/12 | 12/12 | +1 |
| 独立变体 | 12/12 | 12/12 | +0 |
| 边界回归 | 4/4 | 3/4 | -1 |
| 合计 | 54/66 | 49/66 | -5 |

最新完整轮为 49 passed、17 failed，最终 error/judge_error 均为 0。没有最终运行错误不等于没有内部降级：追踪中仍有 10 次 MODEL_RESPONSE_TRUNCATED 和 1 次 MODEL_RESPONSE_INVALID；另有召回时限耗尽被内部降级处理。

这里的“完整”只指当前固定 66 条，不是公开数据集全集。LongMemEval 是 oracle 历史子集及自定义 DeepSeek 评分，不是官方榜单。原清单中的 heldout 已反复使用，应视为已见回归，不能证明未见泛化。

## CLAMBER 的协议 2 独立分析

协议 2 只给本轮已经产生的 24 条回答重新评分，不再次执行 Karen 或工具；独立保存，不覆盖协议 1。旧回答也有独立协议 2 基线，因此可以将标签动作与质量分开比较。

| 指标 | 旧回答协议 2 | 本轮回答协议 2 |
| --- | ---: | ---: |
| 原标签动作一致 | 15/24 | 13/24 |
| 综合（动作与可评分质量） | 15/24 | 12/24 |
| 必须澄清：正确追问 / 漏问 | 6 / 6 | 3 / 9 |
| 无需澄清：正确放行 / 过度追问 | 9 / 3 | 10 / 2 |
| 澄清召回率 | 50% | 25% |
| 过度追问率 | 25% | 16.7% |
| 匹配标签的追问质量 | 6/6 通过 | 2/3 通过 |

减少过度追问的同时，更多标签要求澄清的请求被放行，因此不能宣称整体改善。部分差异是公共信息先查证或并列覆盖与公开标签的冲突；24 条没有独立冻结的产品策略标注，产品策略成绩保持未评分，不能自动把这些差异改成产品通过。

clamber-2050 本轮确实追问，但只询问修正列表还是示例，协议 2 判问题质量失败。该评分解释同时包含“没有真实缺口”的判断，与冻结的预期动作有张力，仍需人工复核。原 failed 保留；即使这一条改判，通过也仅为 13/24，低于旧协议 2 的 15/24。初步校准 12/12 不能保证所有评分可靠。

## 具体改善与退步

改善：clamber-1658、zh-clarification。

退步：clamber-0310、clamber-2813、clamber-1132、cc539528、fea54f57、caf03d32、boundary-distinct-scanner-uses。

主要发现：

1. **精排降级与历史关联耦合。** fea54f57、cc539528、boundary-distinct-scanner-uses 均出现 RECALL_TIMEOUT → RERANK_FAILED_FUSION_ORDER → history.status=unavailable → memory_guard 澄清。召回全流程当前固定为 45 秒，已找到详细记录仍可能因精排未完成而无法使用。需要审查思考精排与整体预算的匹配，并区分读取历史事实和必须唯一选定的旧任务延续。不能把所有降级都要求用户重述。
2. **个性化证据覆盖不足。** caf03d32 给出慢炖锅建议，但未覆盖历史中的酸奶制作兴趣，被判缺少相关个性化内容。
3. **研究请求缺少适用历史背景。** 75832dbd 转为询问研究领域，仍未完成原请求。旧版也失败，不能算新增退步或改善。
4. **公开标签与产品动作边界不同。** 多条 CLAMBER 形成 external_lookup 目标；例如询问《The Resident》演员，本轮选择查证而标签要求提问。该集使用意图适配器，形成查证目标不证明最终查证和交付完成。
5. **中文多轮交付有局部改善。** zh-clarification 本轮补充信息后完成交付，中文回归 12/12；相同场景仍需独立重复，不能单次证明稳定性。

## 可比性与连接故障

原 manifest、标签、参考答案、协议 1 评分及并发 2 保持不变。清单 SHA256：940dbd0a1b10fe65a87aa6c1896036e33db10bdae87172da7a7ae3d8b0e3cfaf。

采用最新产品和配置：意图/路由/清晰度/精排为 DeepSeek low 思考；规划配置 medium（当前适配器实际映射 high）；记忆提取与查询非思考；embedding 为 bge-m3:latest；协议 1 保留原 deepseek-chat 评分。与 10 月 7 日相比，规划配置及执行库源码已有变化，不能单独将差异归因于最后一次澄清改动。模型随机性和实时资料也会影响成绩，这不是旧/新代码在完全相同配置下的随机对照实验。

本轮 Karen 版本 `b01fb19a01ceb266c96c0b97e2897938e3db83f6`；两项目源码运行前后均 stable。DynamicAgentGraph 的 Git revision 相同但工作区含已有修改，因此以源码哈希记录实际执行版本，未重置或编辑这些修改。

- Karen 源码 SHA256：`79bfe078ac8d19405fe6808f2381ef41673366d8b161e50cfda5681a6a1c6eb6`。
- DynamicAgentGraph 源码 SHA256：`ed81abadb8f497617808f8d2b7de0f5ba7bc45537565dc1a751ad33358939dc7`。

此前尝试全部保留，未与主结果拼接：

- 首次完整尝试 full-acceptance-20261009-latest-v1：25/66，17 failed、22 error、2 judge_error，受大量连接及记忆写入错误影响，不用于评价代码改善。
- 第二次尝试 full-acceptance-20261009-latest-repeat-v1：完成 26 条后因同类代理错误主动中断；interrupted.json 明确标为不能用作完整基线。
- 追踪确认 ProxyError、连接重置；加载 .env 后存在小写 http_proxy/https_proxy。直连检查两个模型均成功。最终完整轮仅在评测进程内添加 NO_PROXY/no_proxy，让 api.deepseek.com、localhost、127.0.0.1 绕过代理；未修改 .env、全局代理、模型配置或评分规则。

## 原始材料

以下 runs 路径相对当前工作树，均为本地评测材料，不纳入 Git：

- 有效完整轮：`runs/evaluation/full-acceptance-20261009-latest-direct-v1`。
- 其中 `comparison.json` 保存分组、改善/退步 ID、全部失败、问题来源与 event_id。
- 协议 2：`runs/evaluation/clamber-20261009-latest-direct-v2`。
- 旧回答协议 2：`runs/evaluation/clamber-v2-final-saved-baseline-20261009`。
- 每例 result.json、execution、memory、observability 保留调用、答案、内部降级与诊断；run.json 和 transport.json 记录版本、配置、源码完整性及连接方式。

## 全部未通过案例

| 用例 | 测试集 | 诊断/判定 |
| --- | --- | --- |
| clamber-0191 | CLAMBER | outcome: expected clarification, got goal |
| clamber-0278 | CLAMBER | outcome: expected clarification, got goal |
| clamber-2050 | CLAMBER | 协议 1 评分未认可澄清；协议 2 动作正确、问题质量失败，需人工复核 |
| clamber-0310 | CLAMBER | outcome: expected clarification, got goal |
| clamber-2759 | CLAMBER | outcome: expected complete, got clarification |
| clamber-2664 | CLAMBER | outcome: expected complete, got clarification |
| clamber-2825 | CLAMBER | outcome: expected clarification, got goal |
| clamber-2813 | CLAMBER | outcome: expected clarification, got reply |
| clamber-1132 | CLAMBER | outcome: expected clarification, got goal |
| clamber-0997 | CLAMBER | outcome: expected clarification, got goal |
| clamber-1174 | CLAMBER | outcome: expected clarification, got reply |
| clamber-0730 | CLAMBER | outcome: expected clarification, got reply |
| cc539528 | LongMemEval oracle | outcome: expected complete, got clarification |
| fea54f57 | LongMemEval oracle | outcome: expected complete, got clarification |
| 75832dbd | LongMemEval oracle | outcome: expected complete, got clarification |
| caf03d32 | LongMemEval oracle | 个性化信息覆盖不足：未涉及酸奶制作兴趣 |
| boundary-distinct-scanner-uses | 边界回归 | outcome: expected complete, got clarification；answer missing: 文档；answer missing: 老照片 |
