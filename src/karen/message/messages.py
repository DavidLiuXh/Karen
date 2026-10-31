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
