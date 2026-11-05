# Gmail 只读访问

`src/karen/tools/gmail.py` 封装 Gmail REST API，使用已有的 `httpx`，不增加依赖。
已通过 `tools/gmail_tools.py` 注册为执行图工具，CLI 和微信入口共用此装配。尚未接入授权网页、定时监测进度保存或新邮件通知。

## 授权与使用

在 Google Cloud 项目启用 Gmail API，配置 OAuth 同意屏幕和客户端，通过用户授权获取
`https://www.googleapis.com/auth/gmail.readonly` scope 的 refresh token（离线访问）。
授权流程由调用方完成；不要将 client secret 或 refresh token 写入代码、日志或提交。
本模块不读取环境变量、不保存凭据；调用方显式传入凭据并管理 HTTP 客户端生命周期。

```python
import httpx
from karen.tools.gmail import GmailClient, GmailCredentials

async def read_inbox(client_id, client_secret, refresh_token):
    credentials = GmailCredentials(client_id, client_secret, refresh_token)
    async with httpx.AsyncClient() as http:
        gmail = GmailClient(http, credentials)
        baseline = await gmail.get_profile()  # historyId 可作为监测起点
        page = await gmail.list_inbox()
        for message in page.get("messages", []):
            detail = await gmail.get_message(message["id"])
            # detail 是 Gmail 原始 JSON；正文在 MIME payload 中，以 base64url 编码。
        return baseline["historyId"], page.get("nextPageToken")
```

- `list_inbox(page_token=None, max_results=100)` 返回一页收件箱邮件 ID，包含已读与未读。
  上限 500；将 `nextPageToken` 传回以继续读取，不能把第一页视为完整收件箱。
- `get_message(message_id)` 返回 `format=full` 的邮件详情；大型附件需要独立下载接口，当前未封装。
- `get_profile()` 返回邮箱资料及当前 `historyId`。
- `list_history(start_history_id, page_token=None)` 返回一页原始邮箱变更，不限定标签或事件类型。
  消费方需解析 `messagesAdded`、`labelsAdded` 等事件并检查 `INBOX`，自行去重；不能将所有历史变更视为新邮件。
  处理完全部分页后才持久化新的历史位置。监测与游标保存尚未实现。

客户端首次请求通过 refresh token 换取 access token；401 时刷新并仅重试一次。
每次 HTTP 请求超时 30 秒，不跟随重定向，其他错误不自动重试。
`GmailError` 仅暴露错误码及可选 `status_code`，不包含响应正文或凭据。
历史接口 404 映射为 `GMAIL_HISTORY_EXPIRED`，调用方必须重新同步，不能报告为没有新邮件。
凭据不出现在 dataclass repr 中；调用方仍需避免记录 HTTP 请求和邮件详情。

## 验证与边界

`uv run --no-sync pytest tests/test_gmail.py` 使用 MockTransport 验证请求、分页参数、token 刷新、
过期历史位置和错误脱敏。未使用真实 Gmail 账号完成线上授权或读取验收。

官方协议：[邮件列表](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list)、
[历史增量](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list)、
[同步指南](https://developers.google.com/workspace/gmail/api/guides/sync)、
[OAuth 离线访问](https://developers.google.com/identity/protocols/oauth2/web-server#offline)。

## 执行引擎工具

启动环境同时提供 `GMAIL_CLIENT_ID`、`GMAIL_CLIENT_SECRET`、`GMAIL_REFRESH_TOKEN` 时，自动注册
`gmail.get_profile`、`gmail.list_inbox`、`gmail.get_message`、`gmail.list_history`，版本均为 `1.0.0`。
全部未设置时不启用；部分设置时提示配置不完整并跳过 Gmail，不影响其他能力。
注册阶段不访问网络。凭据由闭包绑定，不进入工具 schema 或参数；HTTP 客户端随渠道退出关闭。
通过 `uv run --env-file .env karen` 显式加载本地配置；配置更新后需重启。

工具参数与上述 Python 方法一致。为符合执行引擎的封闭 JSON schema 契约，工具统一返回
`{"json": "序列化后的 Gmail 原始响应"}`；模型解析其中 JSON 后读取邮件、分页 token 或 historyId。
调用受节点剩余 deadline 约束；网络失败、429、5xx 可交由引擎重试，授权错误与历史游标过期不可重试。
邮件内容属于不可信输入。执行输出会进入正常任务交付和本地执行记录，使用前应了解该隐私边界。

`tests/test_gmail_tools.py` 使用真实 DynamicAgentGraph 与模拟 HTTP 验证注册、调用、只读授权、
凭据不进入记录、错误分类和 deadline；这不代表真实 Gmail OAuth 或自然语言规划已在线验收。
