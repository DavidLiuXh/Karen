"""Planner delivery guidance."""

from ..prompt.prompts import ANSWER_PRESENTATION_INSTRUCTION

WEB_TOOL_SELECTION = """

For web discovery, prefer deepseek.search when authorized. Use tavily.search
when DeepSeek search is unavailable or when a separate search index would help.
Search scores from different providers are not comparable.
Plan at least one targeted evidence path for each material question the user
asks. When the user asks what drove a decision, search for decision-makers'
stated rationale and evidence of actors' actions and influence. Event dates
and status are supporting facts. Assign these same substantive questions to
the source-extraction nodes, including ones reading sources found for fact
verification. From each relevant source retain who did or said what, the
specific effect or plausible mechanism, and the supporting passage and URL.
Carry relevant conflicting accounts into synthesis. A timeline, joint
interests, or a market reaction alone does not establish who drove a decision. For material claims about recent events or causation, plan to read
source text before synthesis. When URLs are discovered at runtime, bind the
whole results array to deepseek.fetch_search_results if authorized. Avoid
indexing a result that may be absent. For known URLs use a single-page reader.
Treat summaries as leads. Preserve relevant leads, supporting passages and
attribution through extraction. Prefer dated event-specific sources over
general profiles when resolving conflicting claims. If a page cannot be read,
distinguish unverified leads and source gaps from evidence against the claim.
For an explicitly requested DeepSeek read or an alternative extraction of a
public webpage, use deepseek.fetch when authorized. Its URL is the requested URL,
and its content is extracted Markdown. Prefer web.read_page for reading or
summarizing visible webpage content, extracting visible links or tables, and
collecting evidence for research. Use web.fetch when the goal requires the raw
response or depends on markup-only evidence, such as iframe src, embedded metadata,
or DOM structure. A large or truncated readable page alone is not a reason to
send its full HTML to a model. For raw responses, prefer extracting the needed
fields before model synthesis when an authorized tool can do so. Bind only the
evidence needed by each downstream node.
"""

FINAL_DELIVERY_PLANNING = """

若规划包含撰写面向用户正文的节点，把以下要求写进该节点的 instruction，
使其无需读取 GoalSpec.context 也能执行：
""" + ANSWER_PRESENTATION_INSTRUCTION + """
证据分级可用于中间分析和输出契约规定的字段。只有用户明确要求按证据等级组织正文时，
才安排这样的章节；有意义的证据强弱用自然语言随对应判断说明。"""

PLANNER_RESPONSE_WITHOUT_RECALL = """用用户要求的语言和粒度直接交付，明确约束与否定限定优先于默认值和适用偏好。
只使用支持本轮目标的证据，保留关键事实及其时间、来源和范围。用户给定的假设按条件使用，区分已核实事实、
待查证项和推断。根据相应时点和可比口径处理变化，保留有实质影响的冲突、缺失、部分覆盖及失败；
不从相关性推定因果，不从历史记录推定本轮授权。
按请求提供必要出处及交付物。保留输出契约要求的 evidence/limitations，面向用户的正文说明影响结论的限制，
省去内部诊断和无关备选，保留用户指定的格式。"""


LOCAL_EDIT_PLANNING = """

For a localized addition or replacement in an existing text file, prefer
file.edit_text when authorized. Read only the relevant regions when possible
and locate an exact unique anchor, checking whether the requested section
already exists. Ask the writing node for only the new text. Bind that text
and the anchor to the file tool; do not send the untouched document to a model
for concatenation or verification. For intermediate edit strings, use plain
string schemas, empty-string initial values and the replace reducer. The
file tool checks the anchor before writing. Use the tool result and a small
targeted check for delivery. Use full-file writing for new files or requested
whole-document rewrites.
"""


PLANNER_BINDING_REPAIR = """
修复 repair_node_index 指向的节点的输入绑定。根据 repair.validation_errors、原图、
目标与已注册能力契约，返回该节点完整的 input_bindings，包括未受影响的绑定。
只返回包含 input_bindings 的对象。程序会保留图的其他部分并校验完整图。
只引用已有且保证可用的输入和状态字段。
"""
