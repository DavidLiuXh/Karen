# 通用提示词整理验证（2026-10-07）

通用规则与 few-shot 整理已完成，但真实模型验证仍存在失败和波动，不能据此声称语义可靠性已解决或整体达标率提高。

产品提示词提交为 `da6183a`；`22f7c47` 只补齐新增评测案例的空评分引用字段。三次运行使用同一产品源码哈希 `f2b047e547607781935faa1f112b4055a5e9ffd72e59bae7192a207eeb51e3c7`，source_integrity 均为 stable。模型、思考模式、预算、协议 1 和原有判定标准不变。

## 实现范围

- 将原邮件、资源整理、爱好、搬家、推荐与报价场景说明提炼为必要信息、证据贡献、偏好程度、事实长期性、计划与实际状态、时间口径等通用规则。
- 共用规则直接复用同一文本定义，没有增加规则引擎、场景分支、模型调用、配置或外部依赖。
- 场景内容只放在有明确标记的 few-shot 块；示例不是本轮事实、来源、授权或额外规则。少量键名映射在记忆示例中保留。
- 保留问题台账、来源与引文核验、一次有界内容修复、输出 schema 和时区契约。记忆提示词版本升至 20。

设计与维护边界见 [通用规则与 few-shot](PROMPT_POLICY.md)。

## 离线验证

最终产品源码下 `pytest -q` 为 **356 passed**，Ruff 与 `git diff --check` 通过。离线测试验证状态、接口和关键边界，不能替代真实模型的语义测试。

## 真实运行的原始结果

| 独立运行 | 总数 | passed | failed | judge_error | error |
| --- | ---: | ---: | ---: | ---: | ---: |
| prompt-generalization-v1 | 23 | 19 | 2 | 2 | 0 |
| prompt-generalization-v1-repeat | 6 | 3 | 2 | 1 | 0 |
| prompt-generalization-v1-metadata-fixed | 12 | 10 | 1 | 0 | 1 |

首轮 23 条由原有八条澄清契约、三条既有回归和新增 12 条组成。原有 11 条为 9 passed、2 failed；新增 12 条为 10 passed、2 judge_error。重复运行选取六条重点案例，结果独立保留，不与首轮拼接。

首轮新增案例的 `gold` 缺少意图适配器所需的 `clarifying_question:null`，两条已返回澄清的记录在 grading 阶段因 KeyError 成为 judge_error；重复时一条再次发生相同异常。修正只增加这个空评分引用，没有改变用户输入、历史、期望 outcome、关键词或任何原有判定。修正后对完整新增 12 条独立重跑，原异常记录不改成 passed。

新增案例属于开发者在规则整理后编写的迁移回归，没有进入生产 few-shot；不宣称独立盲测。九条借用 CLAMBER 意图适配器，只验证路由、澄清及 GoalSpec，不证明外部操作已完成；另外三条包含真实记忆提取、持久化、重启、召回和直接回应。

## 确认的结果与剩余问题

1. **状态流程和若干迁移边界通过。** 部分回答、无效续答、取消、切换任务、要求修正不可行任务与仅分析不可行性的区分均有通过记录。中文条款整理、必要文件定位、公开名称查证和任务时区日期通过。原物态分类本轮没有再追问温压，爱好读取也通过；单次通过不证明长期稳定。
2. **原邮件过度澄清仍存在。** `zh-clarification` 首轮与重复均失败。用户已给收件人、进度并要求只给正文草稿，模型仍将项目名称保留为必要问题。通用规则和 not_required 退出路径未能稳定纠正模型的必要性判断。
3. **封闭条件解释仍有波动。** `contract-closed-incompatible-v2` 首轮返回 YES，被判 failed；追踪显示模型将“所有样例共享同一条规则”改读为不同样例分别满足析取条件。独立重复返回澄清并通过。没有因某次通过删除失败，也没有为该题增加专用规则。新增跨领域封闭条件案例在评分元数据修正后通过。
4. **英文目标语言不稳定。** 新增 `prompt-generalization-optional-en-v1` 首轮通过，重复和元数据修正后的重跑均因 operative GoalSpec 缺少冻结英文关键词 samples、Tuesday 而 failed。实际目标保留了对应语义，但写成中文。原判定不变；这说明目标语言一致性不足，不等于已经证明最终执行产物会用错语言。该例使用意图适配器，没有端到端执行证据，也不能据此认定中文提示词就是原因。
5. **记忆迁移有通过记录，但出现一次服务中断。** 兼容回复偏好、未来转岗不覆盖当前岗位、后续进度更新计算，首轮三条均通过。重跑前两条再次通过；第三条在 interaction 阶段由 intent_router 连续返回 MODEL_UNAVAILABLE，最终记为 error。它不是记忆提取或评分错误，也不能被首轮成功抵消。

本次没有修改公开标签、评分协议或 DynamicAgentGraph，没有围绕失败题追加场景专用规则。尚未对本次版本重跑固定 66 条，因此此前 **54/66** 仍是旧提示词版本的完整结果，不能作为本版本成绩。

## 复核与复现

新增固定输入：`tests/fixtures/evaluation_prompt_generalization_v1.json`。各目录保存完整 manifest、run.json、results.jsonl、逐例结果及观测追踪：

- `runs/evaluation/prompt-generalization-v1`
- `runs/evaluation/prompt-generalization-v1-repeat`
- `runs/evaluation/prompt-generalization-v1-metadata-fixed`

可用已保存的 manifest 独立重跑，output 必须指定未存在的新目录，例如：

```bash
uv run --env-file .env python -m karen.evaluation run \
  --manifest runs/evaluation/prompt-generalization-v1-metadata-fixed/manifest.json \
  --output runs/evaluation/prompt-generalization-next-run \
  --concurrency 2
```

所有目录均为隔离评测数据，不使用用户全局记忆目录。
