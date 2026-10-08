"""Owner-bound QR login and private credential persistence."""

import asyncio
import fcntl
import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

import qrcode

from .api import API_URL, ILinkClient, WeixinError, trusted_url


@dataclass(frozen=True, repr=False)
class Credentials:
    token: str
    account_id: str
    owner_id: str
    base_url: str

    def __post_init__(self):
        if not all(isinstance(v, str) and v.strip() for v in asdict(self).values()):
            raise WeixinError("WEIXIN_CREDENTIALS_INVALID")
        trusted_url(self.base_url, base=True)


def private_root(root):
    root = Path(root).expanduser().absolute()
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise WeixinError("WEIXIN_SYMLINK_STORAGE")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    return root


def load_credentials(root):
    path = private_root(root) / "credentials.json"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        raise WeixinError("WEIXIN_CREDENTIALS_UNREADABLE") from None
    try:
        with os.fdopen(fd) as handle:
            os.fchmod(handle.fileno(), 0o600)
            data = json.load(handle)
        return Credentials(**data)
    except (TypeError, ValueError):
        raise WeixinError("WEIXIN_CREDENTIALS_INVALID") from None


def acquire_lock(root):
    root = private_root(root)
    fd = os.open(root / "channel.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    handle = os.fdopen(fd, "a")
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise WeixinError("WEIXIN_CHANNEL_IN_USE") from None
    except BaseException:
        handle.close()
        raise
    return handle


def save_credentials(root, credentials):
    root = private_root(root)
    destination = root / "credentials.json"
    if destination.is_symlink():
        raise WeixinError("WEIXIN_SYMLINK_STORAGE")
    fd, name = tempfile.mkstemp(dir=root, prefix=".credentials-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(asdict(credentials), handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, destination)
        dir_fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        Path(name).unlink(missing_ok=True)


def display_qr(content):
    qr = qrcode.QRCode(border=2)
    qr.add_data(content)
    qr.make(fit=True)
    print("请使用本人微信扫描二维码，并在手机上确认绑定 Karen：", file=sys.stderr)
    qr.print_ascii(out=sys.stderr, invert=True)


async def login(http, *, show_qr=display_qr, read_code=None, previous=None):
    try:
        async with asyncio.timeout(300):
            return await _login(http, show_qr=show_qr, read_code=read_code, previous=previous)
    except TimeoutError:
        raise WeixinError("WEIXIN_LOGIN_TIMEOUT") from None


async def _login(http, *, show_qr, read_code, previous):
    """One bounded login; the owner is taken from the confirmed server response."""
    client = ILinkClient(http)
    deadline = asyncio.get_running_loop().time() + 300
    refreshes, verification_attempts = 0, 0
    verify_code = None
    qr = None
    while asyncio.get_running_loop().time() < deadline:
        if qr is None:
            client.base_url = API_URL
            data = await client.request("ilink/bot/get_bot_qrcode", params={"bot_type": 3},
                                        body={"local_token_list": [previous.token] if previous else []},
                                        login=True)
            qr = data.get("qrcode")
            content = data.get("qrcode_img_content")
            if not isinstance(qr, str) or not isinstance(content, str) or not qr or not content:
                raise WeixinError("WEIXIN_QR_INVALID")
            show_qr(content)
        params = {"qrcode": qr}
        if verify_code:
            params["verify_code"] = verify_code
        try:
            status = await client.request("ilink/bot/get_qrcode_status", params=params,
                                          timeout=15, login=True)
        except WeixinError as exc:
            if not exc.retryable:
                raise
            await asyncio.sleep(1)
            continue
        state = status.get("status")
        if state == "confirmed":
            try:
                credentials = Credentials(status["bot_token"], status["ilink_bot_id"],
                                          status["ilink_user_id"], status.get("baseurl") or API_URL)
            except KeyError:
                raise WeixinError("WEIXIN_LOGIN_INCOMPLETE") from None
            if previous and credentials.owner_id != previous.owner_id:
                raise WeixinError("WEIXIN_OWNER_CHANGED")
            return credentials
        if state == "need_verifycode":
            verification_attempts += 1
            if not read_code or verification_attempts > 3:
                raise WeixinError("WEIXIN_VERIFICATION_REQUIRED")
            verify_code = (await read_code()).strip()
            if not verify_code.isdecimal() or len(verify_code) > 16:
                raise WeixinError("WEIXIN_VERIFICATION_INVALID")
            continue
        if state in {"expired", "verify_code_blocked"}:
            refreshes += 1
            if refreshes > 3:
                raise WeixinError("WEIXIN_QR_EXPIRED")
            qr, verify_code = None, None
        elif state == "scaned_but_redirect":
            host = status.get("redirect_host")
            if not isinstance(host, str) or "/" in host:
                raise WeixinError("WEIXIN_REDIRECT_INVALID")
            client.base_url = trusted_url(f"https://{host}", base=True)
        elif state == "binded_redirect":
            if previous:
                return previous
            raise WeixinError("WEIXIN_LOGIN_REQUIRED")
        elif state == "scaned":
            verify_code = None
        elif state != "wait":
            raise WeixinError("WEIXIN_LOGIN_STATUS_INVALID")
        await asyncio.sleep(1)
    raise WeixinError("WEIXIN_LOGIN_TIMEOUT")
