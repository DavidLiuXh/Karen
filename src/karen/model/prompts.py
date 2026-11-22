"""Planner delivery guidance."""

from ..prompt.prompts import ANSWER_PRESENTATION_INSTRUCTION

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
