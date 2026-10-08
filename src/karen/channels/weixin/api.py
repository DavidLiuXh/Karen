"""iLink HTTP transport, without agent execution or hidden request retries."""

import asyncio
import base64
import json
import secrets
from urllib.parse import urlsplit

import httpx

API_URL = "https://ilinkai.weixin.qq.com"
CDN_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
# Wire format verified against Tencent/openclaw-weixin 2.4.9, commit 24de5c9.
PROTOCOL_VERSION = "2.4.9"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 10 * 1024 * 1024


class WeixinError(Exception):
    def __init__(self, code, *, retryable=False, http_status=None):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.details = {"http_status": http_status}


def trusted_url(value, *, base=False):
    """Credentials and media may only reach HTTPS endpoints in Weixin's domain."""
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        valid = (parts.scheme == "https" and host.endswith(".weixin.qq.com")
                 and parts.port in {None, 443} and not parts.username and not parts.password
                 and not parts.fragment and (not base or not parts.query))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise WeixinError("WEIXIN_UNTRUSTED_URL")
    return value.rstrip("/") if base else value


class ILinkClient:
    def __init__(self, http, *, token=None, base_url=API_URL):
        self.http = http
        self.token = token
        self.base_url = trusted_url(base_url, base=True)
        self._poll_timeout = 40

    async def request(self, path, *, body=None, params=None, timeout=15, login=False):
        headers = {"iLink-App-Id": "bot", "iLink-App-ClientVersion": str(0x020409)}
        if body is not None:
            headers.update({
                "Content-Type": "application/json", "AuthorizationType": "ilink_bot_token",
                "X-WECHAT-UIN": base64.b64encode(str(secrets.randbits(32)).encode()).decode(),
            })
            if not login:
                if not self.token:
                    raise WeixinError("WEIXIN_LOGIN_REQUIRED")
                headers["Authorization"] = f"Bearer {self.token}"
                body = {**body, "base_info": {
                    "channel_version": PROTOCOL_VERSION, "bot_agent": "Karen/0.1.0",
                }}
        raw, _ = await self.transfer(
            "POST" if body is not None else "GET", f"{self.base_url}/{path}",
            headers=headers, json=body, params=params, timeout=timeout,
        )
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError
        except (ValueError, UnicodeError):
            raise WeixinError("WEIXIN_RESPONSE_INVALID") from None
        for name in ("ret", "errcode"):
            code = data.get(name, 0)
            if code not in (0, None):
                if code == -14:
                    raise WeixinError("WEIXIN_SESSION_EXPIRED")
                raise WeixinError("WEIXIN_API_REJECTED")
        return data

    async def transfer(self, method, url, *, limit=MAX_RESPONSE_BYTES, **kwargs):
        trusted_url(url)
        try:
            async with asyncio.timeout(kwargs.get("timeout", 15)):
                async with self.http.stream(method, url, follow_redirects=False, **kwargs) as response:
                    status = response.status_code
                    if not 200 <= status < 300:
                        raise WeixinError("WEIXIN_HTTP_FAILED", http_status=status,
                                          retryable=status == 429 or status >= 500)
                    content = bytearray()
                    async for part in response.aiter_bytes():
                        content.extend(part)
                        if len(content) > limit:
                            raise WeixinError("WEIXIN_RESPONSE_TOO_LARGE")
                    return bytes(content), response.headers
        except (httpx.HTTPError, TimeoutError):
            raise WeixinError("WEIXIN_NETWORK_FAILED", retryable=True) from None

    async def updates(self, cursor):
        result = await self.request("ilink/bot/getupdates", body={"get_updates_buf": cursor},
                                    timeout=self._poll_timeout)
        milliseconds = result.get("longpolling_timeout_ms")
        if type(milliseconds) is int and milliseconds > 0:
            self._poll_timeout = min(65, milliseconds / 1000 + 5)
        return result

    async def send(self, peer, item, *, context_token, client_id, run_id=None):
        if not context_token:
            raise WeixinError("WEIXIN_CONTEXT_REQUIRED")
        msg = {"from_user_id": "", "to_user_id": peer, "client_id": client_id,
               "message_type": 2, "message_state": 2, "context_token": context_token,
               "item_list": [item]}
        if run_id:
            msg["run_id"] = run_id
        result = await self.request("ilink/bot/sendmessage", body={"msg": msg})
        # Do not infer acknowledgement from an empty/invalid success response.
        if result.get("ret") != 0:
            raise WeixinError("WEIXIN_SEND_UNCONFIRMED", retryable=True)
        return result

    async def typing(self, peer, context_token, *, active):
        config = await self.request("ilink/bot/getconfig", body={
            "ilink_user_id": peer, "context_token": context_token,
        }, timeout=10)
        ticket = config.get("typing_ticket")
        if ticket:
            await self.request("ilink/bot/sendtyping", body={
                "ilink_user_id": peer, "typing_ticket": ticket, "status": 1 if active else 2,
            }, timeout=10)

    async def notify(self, *, started):
        action = "notifystart" if started else "notifystop"
        await self.request(f"ilink/bot/msg/{action}", body={}, timeout=5)


async def retry_delay(attempt):
    await asyncio.sleep(min(30, 2 ** min(attempt, 5)))
