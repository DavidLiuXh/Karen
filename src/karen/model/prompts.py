"""Planner delivery guidance."""

from ..prompt.prompts import ANSWER_PRESENTATION_INSTRUCTION

WEB_TOOL_SELECTION = """

For web discovery, honor any search provider explicitly requested by the user
when authorized. Default to {preferred} when authorized and available. If it is unavailable, use {alternate}
when authorized. Use a separate search index when independent coverage is needed.
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
For in-depth research with material gaps that may only become clear after
reading the first sources, plan one optional follow-up when deepseek.search_followup
is authorized and compatible with the requested search provider. Have a model assess the initial
source evidence against the requested deliverables. It should emit a required string
query (maxLength 128), empty if the evidence is sufficient. If an expected
primary record could settle a material decision date or rationale and only
secondary accounts were obtained, treat that as a gap. Keep decision,
announcement and effective dates distinct in the evidence. Use a targeted query
for the most consequential missing fact or conflict. Bind that query to
deepseek.search_followup and bind its entire results array to
deepseek.fetch_search_results. Give synthesis the first-pass evidence and the
follow-up pages; require it to explain any material unresolved gap. Skip this
sequence for straightforward lookups.
Treat summaries as leads. Preserve relevant leads, supporting passages and
attribution through extraction. Prefer dated event-specific sources over
general profiles when resolving conflicting claims. If a page cannot be read,
distinguish unverified leads and source gaps from evidence against the claim.
Honor an explicitly requested fetch tool when authorized and appropriate.
Default to deepseek.fetch for readable webpage content at a known URL when
authorized. It returns Markdown, the requested URL, and a truncation flag.
Use web.read_page when DeepSeek Fetch is unavailable or when its final URL
or extraction metadata is needed. Use web.fetch for original markup or DOM
evidence when that tool is registered and authorized. Bind only the evidence
each node needs. Treat batch pages with error as missing evidence.
Prefer primary documents or official filing repositories. Verify that each
source supports the requested facts and report material gaps.
These text tools cannot read PDFs. An older filing cannot substitute for a
requested reporting period.
"""

FINAL_DELIVERY_PLANNING = """

若规划包含撰写面向用户正文的节点，把以下要求写进该节点的 instruction，
使其无需读取 GoalSpec.context 也能执行：
""" + ANSWER_PRESENTATION_INSTRUCTION + "\n"


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


WEB_TOOL_SELECTION += """

When using the authorized deepseek.search_followup for optional research, have each
existing evidence-extraction worker emit a separate coverage field alongside its full
evidence in the same call. The decision must allow an empty query when evidence suffices.
Bind it to deepseek.search_followup, whose empty-query behavior skips the network call.
Use a string with maxLength 768 for coverage. Tie coverage to the requested questions:
record key findings with source URLs, material conflicts, and missing or weak support.
If a branch has no usable evidence, say what could not be established. Map coverage and
full evidence to separately addressable state fields. Bind only the coverage fields and
the relevant goal constraints to the follow-up decision. Do not bind raw search or page
results or full extracted evidence to that decision. Bind the full source-backed evidence
and any follow-up evidence to final synthesis, retaining quotations, citations and caveats.
Coverage notes are for the follow-up decision; the full evidence carries the same facts and
limitations to synthesis without repeating those notes.
"""
