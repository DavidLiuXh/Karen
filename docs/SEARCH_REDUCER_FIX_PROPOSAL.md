# 并行搜索结果合并冲突：修复说明

## 已确认原因

2026-10-03 北京路亚推荐任务，run_id 为 `cdbd57e331fa493d8b30258f51e37e28`。
三个并行 Tavily 搜索节点均写入 `search_results`，使用 `builtin.merge_by_key`，唯一键为 `url`。
该 reducer 要求相同键对应的完整条目相同。同一 URL 在不同搜索中返回的 score、content 可以不同，
因此这张图的聚合设计不符合工具结果的实际语义。三个搜索都已返回，状态提交失败，汇总节点未执行。

例如 `https://book.catches.com/zh/luya-course04.html` 同时出现于 search_spots 与 search_species，
对应 score 分别为 0.643173 和 0.6934942；另外三个重复 URL 的 content 和 score 都存在差异。
Reducer 的拒绝行为符合现有契约，不应通过忽略差异、覆盖结果或无条件重跑来规避。

## 已完成的离线验证

用三份已保存节点产物作为离线工具响应，不访问 Tavily、不调用真实 LLM，也不改写原执行记录：

- 原图重放稳定失败，诊断为 REDUCER_FAILED / key_conflict，worker 未调用。
- 搜索节点分别写 search_spots_results、search_species_results、search_access_results，均使用单写者 builtin.replace。
- 汇总节点保持等待三个搜索的依赖，并分别绑定三个结果数组。
- 修正后的图执行 COMPLETED、output_complete=true、没有诊断；汇总收到完整的 10、10、9 条记录。
  校验了每组数组与原始产物完全相同，保留同 URL 的不同查询观察。

离线成功验证的是结构与数据传递，不是路亚资源推荐内容的业务验收。

## 已实施的 DynamicAgentGraph 修改

用户已于 2026-10-04 确认实施以下修改。

1. `src/dynamic_graph/tools/tavily.py` 的能力描述补充真实数据保证：score 是对当前 query 的相关性评分，
   content 也是查询相关片段；同一 URL 在不同查询中的完整结果条目不保证一致。
   保持工具名称、版本、输入输出 schema 和执行行为不变。
2. 中英文 `src/dynamic_graph/planning/prompts/planner_system_v1*.txt` 补充通用规划规则：
   多个分支可能返回同一实体的不同观察时，分别保存分支结果，或使用确实标识观察记录的稳定键；
   不能仅凭实体 ID（如 URL）认为完整观察值必然相同。
   对多查询搜索，采用各分支完整数组的 replace 字段，汇总节点读取全部分支。
3. 在引擎集成测试中增加合成的并行搜索案例：相同 URL、不同 score/content，原错误图应拒绝；
   独立字段图应完整保留各组结果并完成汇总。继续保证严格 reducer 对真实键冲突的拒绝。
4. 用完全合成的数据做真实 planner 验证，检查生成图采用安全的数据组织并完成离线执行；
   不将此次真实观测轨迹或记忆记录发送给外部模型。

不修改 reducer 的严格语义，不新增 reducer，不改变默认能力授权，不加入自动重跑。
基于 DynamicAgentGraph 现有未提交修改追加了工具说明、中英文规划规则和独立的
`tests/integration/test_parallel_search_results.py`，保留了原有工作。
任务图由 planner 生成；本次通过能力描述和规划规则指导新图采用独立字段，没有改写历史任务图，
也没有在 Karen 中另加图重写逻辑。

## 实施后的验证

- DynamicAgentGraph 完整测试：278 项通过；新增 8 个并行搜索回归用例。
  覆盖正反完成顺序、空分支、同 URL 仅评分不同、仅片段不同、两者不同，以及多个写者共用 replace 的拒绝。
  汇总前分别核对所有分支原始数组，确认没有结果被覆盖或丢失。
- Karen 兼容性回归：133 项在沙箱内通过；只读 HTTP 服务测试因沙箱禁止绑定回环端口，
  在允许绑定本机端口的环境单独重跑通过，共 134 项通过。
- 真实 DeepSeek 中英文 planner 与 worker 验证：使用脚本内构造的目标和工具响应，
  不读取用户历史、记忆或真实任务日志，不调用 Tavily。
  两次规划均生成 3 个独立的 replace 结果字段；汇总输入与输出均保留各查询完整的 2、2、2 条记录，
  包括同一 URL 的不同评分和片段，执行 COMPLETED。
- 修改文件 Ruff 检查、格式检查及 Git 差异空白检查通过。

这是规划规则修正，保留现有结构校验和严格 reducer 拒绝行为。模型若仍生成不安全的图，
会按原契约返回诊断。已运行的 Karen 进程需要重新启动，以重新注册工具说明并加载规划提示词。
