"""User-facing copy shared by terminal, Weixin and observation entry points.

Model instructions and schema descriptions belong to their domain prompts modules.
Templates use named format fields; callers supply data without changing the wording.
"""

# Terminal and CLI
KAREN_PREFIX = "Karen："

INPUT_PROMPT = "你："

INVALID_TIMEZONE = (
    "Karen：无法确定有效的用户时区，请使用 --timezone 指定 IANA 时区，如 Asia/Shanghai。"
)

OBSERVATION_STORAGE_UNAVAILABLE = "Karen：观测记录目录无法打开，本次未启动对话。"

OBSERVATION_PORT_UNAVAILABLE = (
    "Karen：无法启动观测页面，端口 {port} 可能已被占用；请使用 --observe 指定其他端口。"
)

OBSERVATION_STARTED = "Karen 观测页面：http://127.0.0.1:{port}/"

TAVILY_UNAVAILABLE = "Karen：未配置 TAVILY_API_KEY，Tavily 网络搜索暂不可用。"

MEMORY_STORAGE_UNAVAILABLE = (
    "Karen：记忆目录无法安全打开，可能已有进程使用或文件损坏。本次未启动对话。"
)

WEIXIN_STARTED = "Karen：个人微信渠道已启动，仅处理扫码绑定者的私聊。Ctrl+C 退出。"

CLI_TURN_FAILED = "Karen：本轮未完成（{code}），请重新输入。"

MEMORY_WRITE_FAILED = "Karen：记忆写入未完成（{warning}）。"

EXIT_PERSISTENCE_FAILED = "Karen：部分对话未能持久化，退出失败。"

WEIXIN_VERIFICATION_PROMPT = "请在下面输入微信要求的验证码："

WEIXIN_LOGIN_SUCCESS = "Karen：微信绑定成功。凭据已保存在 ~/.Karne/weixin（仅当前用户可读）。"

CLI_DESCRIPTION = "Karen 任务与意图澄清"

CLI_JSON_HELP = "展示完整执行结果 JSON"

CLI_TIMEZONE_HELP = "用户的 IANA 时区，默认读取本机时区"

CLI_WECHAT_HELP = "通过个人微信对话，首次启动扫码绑定"

CLI_WECHAT_LOGIN_HELP = "仅扫码绑定或刷新微信凭据，不启动模型"

CLI_OBSERVE_HELP = "同时启动本地观测页面，默认端口 {port}，可指定其他端口"

CLI_INVALID_OBSERVE_PORT = "--observe 端口必须在 1–65535 之间"

CLI_INVALID_LOGIN_FLAGS = "--wechat-login 单独使用；绑定后用 --wechat 启动"

CLI_INVALID_JSON_CHANNEL = "--json 仅适用于命令行对话"

WEIXIN_CHANNEL_FAILED = "Karen：微信渠道未能继续运行（{code}）。"

WEIXIN_LOGIN_REQUIRED = "请执行 karen --wechat-login 重新扫码，再启动 --wechat。"

# Weixin delivery
WEIXIN_QR_LOGIN = "请使用本人微信扫描二维码，并在手机上确认绑定 Karen："

WEIXIN_PROGRESS = "已收到，任务仍在处理中，完成后我会回复。"

WEIXIN_INTERRUPTED = "上次处理因进程中断而停止，无法确认任务是否完成。为避免重复操作，我没有自动重做；请先核对结果，再决定是否重新发起任务。"

WEIXIN_REQUEUED = "已重新排队 {count} 条未送达消息。"

WEIXIN_QUEUE_STATUS = "待处理任务：{pending_tasks}；待发送消息：{pending_messages}；发送失败：{failed_messages}。发送失败可用 /retry 重试，不会重做任务。"

WEIXIN_MODEL_FAILED = "本轮未完成（{code}）。"

WEIXIN_TURN_FAILED = "本轮未完成（{code}），请重新发送请求。"

# Execution results
TOOL_TRANSPORT_FAILED = "工具服务连接或传输失败，请检查网络后重试。"

TOOL_RESPONSE_INVALID = "工具服务返回了无法解析的数据。"

TOOL_UNAUTHORIZED = "工具服务拒绝授权（HTTP {status}），请检查对应服务的凭据或权限。"

TOOL_RATE_LIMITED = "工具服务触发限流（HTTP 429），请稍后重试。"

TOOL_UNAVAILABLE = "工具服务暂时不可用（HTTP {status}），请稍后重试。"

TOOL_HTTP_FAILED = "工具服务返回 HTTP {status}，本次调用未完成。"

TASK_FAILED = "任务执行失败。"

TASK_CANCELLED = "任务已取消。"

TASK_INCOMPLETE = "执行已结束，但结果不完整。"

PARTIAL_ANSWER = "已产生的部分回答："

SOURCES_LABEL = "来源："

LIMITATIONS_LABEL = "限制说明："

EMPTY_RESULT = "执行已结束，没有返回内容。"

WARNING_LABEL = "提示"

DIAGNOSTIC_LABEL = "诊断"

SOURCE_ITEM = "- {source}：{text}"

LIMITATION_ITEM = "- {description}"

NAMED_OUTPUT = "{name}：\n{text}"

DIAGNOSTIC_ITEM = "{label}（{code}）：{message}"

# Observation entry points and checks
OBSERVATION_CLI_DESCRIPTION = "Karen 本地运行观测页面（只读）"

OBSERVATION_OPEN_HELP = "用本机浏览器打开页面"

OBSERVATION_URL = "Karen 观测页面：{url}"

CHECK_CLARIFICATION_EXECUTION = "澄清轮未启动执行"

CHECK_HISTORY_SELECTION = "历史仅在关联确认后注入"

CHECK_RERANK_FALLBACK = "精排失败未冒充精排成功"

CHECK_DIRECT_EXECUTION = "直接回应与取消未启动执行"

# Clarification and memory feedback
AMBIGUOUS_REFERENCE = "‘{expression}’指的是哪一个：{candidates}？"

SELECTION_CRITERIA_QUESTION = "请说明选择时最看重的因素或相关偏好。"

CANCELLED_PENDING_TASK = "好的，已取消当前待澄清的任务。"

MEMORY_READ_INCOMPLETE = "本轮记忆读取未能完成。"

HISTORY_LINK_UNVERIFIED = "关联核验未完成，不能确定所指任务。"

# Concurrent task interaction
TASK_HELP = "/new 内容：新任务；/tasks：列表；/use ID：选中任务；/reply ID 内容：回复；/cancel ID：取消；/schedule：定时任务；/exit：退出。未指定任务且有待回答问题时，请先选择任务。"
TASK_ACCEPTED = "[{task_id}] 已接收，正在排队或处理。"
TASK_LIST_ITEM = "[{task_id}] {state} · {title}"
TASK_LIST_EMPTY = "当前没有任务。"
TASK_UNKNOWN = "没有找到这个任务，请用 /tasks 查看任务编号。"
TASK_FOCUSED = "已选中任务 [{task_id}]；后续普通输入属于此任务，使用 /new 发起无关任务；任务结束后需重新指定。"
TASK_CANCEL_REQUESTED = "[{task_id}] 已请求取消。"
TASK_NOT_WAITING = "该任务当前不等待回答；运行中的任务请等待或取消，其他请求请使用 /new。"
TASK_SELECT_OWNER = "这条输入已保留。请用 /use ID 指定它回答的任务；若它是新请求，用 /new 直接将这条输入建为新任务。"
TASK_SELECT_PENDING = "还有一条输入等待关联，请先用 /use ID 指定任务，再发送其他补充。"
TASK_INPUT_REJECTED = "任务输入未接收（{code}）。"
TASK_EVENT = "[{task_id} · {state}]\n{text}"
TASK_ERROR = "本轮未完成（{code}）。任务保留在列表中；第一版不自动重放失败任务。"
TASK_CANCELLED = "任务已取消；已发生的外部操作不会回滚。"
TASK_CONCURRENCY_HELP = "同时推进的任务数，默认 {count}；独立执行图可并行，同一文件的冲突操作等待"
TASK_CONCURRENCY_INVALID = "--max-concurrent-tasks 必须为正整数"
TASK_STATE_LABELS = {
    "queued": "排队中", "running": "处理中", "waiting": "等待回复",
    "completed": "已完成", "failed": "失败", "cancelled": "已取消",
}

# Native scheduled tasks
SCHEDULE_UNAVAILABLE = "定时任务存储未打开，本次未接受操作。"
SCHEDULE_EMPTY = "还没有定时任务。用 /schedule 每天中午12点生成GitHub趋势的精美HTML页面 新增；/schedule 后也可用自然语言修改、删除或立即执行。"
SCHEDULE_CREATED = "已保存定时任务。仅在 Karen 运行期间触发。"
SCHEDULE_UPDATED = "已更新定时任务，正在执行的任务继续使用原来的内容。"
SCHEDULE_DELETED = "已删除定时任务 [{schedule_id}]；已经开始的执行仍可通过 /cancel 执行任务编号 取消。"
SCHEDULE_TRIGGERED = "已触发定时任务 [{schedule_id}]，执行任务编号 [{task_id}]。原定时间不变。"
SCHEDULE_CLARIFY = "{question}\n请用 /schedule 重新发送包含所需信息的完整操作。"
SCHEDULE_FAILED = "定时任务操作未完成（{code}），请检查后重新发送。"
SCHEDULE_NO_NEXT = "无（单次规则已到期或已触发）"
SCHEDULE_DAILY = "每天 {time}"
SCHEDULE_WEEKLY = "每周{day} {time}"
SCHEDULE_INTERVAL = "每隔 {minutes:g} 分钟"
SCHEDULE_ITEM = "[{schedule_id}] {name}\n规则：{timing}（{timezone}）\n下次触发：{next_time}\n最近执行：{state}；执行任务编号：{task_id}；最近跳过：{skipped}\n执行内容：{prompt}"
SCHEDULE_EVENT = "[定时任务 {schedule_id} · {name}]\n{text}"
SCHEDULE_START_FAILED = "Karen：定时任务存储无法打开，本次未启动对话。"
SCHEDULE_STATE_LABELS = {
    "not_run": "尚未执行", "interrupted": "上次执行因退出中断，未自动重做",
    "rejected": "未接收执行，请检查任务队列或交付渠道",
}
SCHEDULE_SKIP_LABELS = {"busy": "上一轮尚未结束", "missed": "离线期间错过，未补跑"}
SCHEDULE_ERRORS = {
    "SCHEDULE_NOT_FOUND": "未找到该定时任务。请用 /schedule list 查看编号或完整名称。",
    "SCHEDULE_TARGET_AMBIGUOUS": "存在同名定时任务，请用 /schedule list 查看后指定编号。",
    "SCHEDULE_EXPLICIT_TARGET_REQUIRED": "请在 /schedule 操作中写出完整任务名称或编号，可用 /schedule list 查看。",
    "SCHEDULE_BUSY": "该定时任务上一轮仍在排队、执行或等待回复，请先处理它。",
    "SCHEDULE_PAST_TIME": "单次执行时间必须在将来，请明确日期和时间。",
    "SCHEDULE_LIMIT": "最多保存 100 项定时任务，请先删除不再需要的规则。",
    "SCHEDULE_CONTEXT_TOO_LARGE": "这次操作的任务内容过多，请用 /schedule list 查看后指定单个任务编号。",
}
