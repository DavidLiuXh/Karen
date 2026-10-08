import base64
import json
import os

import httpx
import pytest

from karen.channels.weixin import ILinkClient, WeixinError, auth, media
from karen.channels.weixin.api import API_URL, MAX_FILE_BYTES, trusted_url
from karen.channels.weixin.auth import Credentials


@pytest.fixture
def credentials():
    return Credentials("fake-bearer", "bot-account", "owner", API_URL)


@pytest.mark.parametrize("url", [
    "http://ilinkai.weixin.qq.com", "https://evil.example", "https://weixin.qq.com.evil.example",
    "https://ilinkai.weixin.qq.com:444", "https://token@ilinkai.weixin.qq.com",
    "https://ilinkai.weixin.qq.com/#secret", "https://127.0.0.1/",
])
def test_transport_restricts_hosts(url):
    with pytest.raises(WeixinError, match="UNTRUSTED_URL"):
        trusted_url(url)


async def test_auth_headers_and_stable_delivery_identity(credentials):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"ret": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = ILinkClient(http, token=credentials.token)
        await client.updates("opaque-cursor")
        item = {"type": 1, "text_item": {"text": "中文回复"}}
        for _ in range(2):
            await client.send("owner", item, context_token="context-secret", client_id="same-id")
    body = json.loads(requests[0].content)
    assert body["get_updates_buf"] == "opaque-cursor"
    assert body["base_info"]["channel_version"] == "2.4.9"
    assert requests[0].headers["authorization"] == "Bearer fake-bearer"
    assert base64.b64decode(requests[0].headers["x-wechat-uin"]).isdigit()
    messages = [json.loads(r.content)["msg"] for r in requests[1:]]
    assert messages[0] == messages[1]
    assert messages[0]["item_list"] == [item]
    assert messages[0]["context_token"] == "context-secret"


@pytest.mark.parametrize("status,body,code,retryable", [
    (200, {"ret": -14}, "WEIXIN_SESSION_EXPIRED", False),
    (200, {"ret": 1}, "WEIXIN_API_REJECTED", False),
    (429, {}, "WEIXIN_HTTP_FAILED", True),
    (500, {}, "WEIXIN_HTTP_FAILED", True),
    (403, {}, "WEIXIN_HTTP_FAILED", False),
    (302, {}, "WEIXIN_HTTP_FAILED", False),
    (200, [], "WEIXIN_RESPONSE_INVALID", False),
])
async def test_transport_classifies_errors_without_leaking_responses(status, body, code, retryable):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(status, json=body, headers={"location": "https://evil.example"})
    )) as http:
        with pytest.raises(WeixinError) as failed:
            await ILinkClient(http, token="secret").updates("")
    assert str(failed.value) == code
    assert failed.value.retryable is retryable


async def test_transport_bounds_stream_and_requires_explicit_ack():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=b"12345")
    )) as http:
        with pytest.raises(WeixinError, match="TOO_LARGE"):
            await ILinkClient(http).transfer("GET", API_URL, limit=4)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={})
    )) as http:
        with pytest.raises(WeixinError, match="UNCONFIRMED"):
            await ILinkClient(http, token="secret").send(
                "owner", {}, context_token="ctx", client_id="id",
            )


async def test_poll_timeout_adapts_to_bounded_server_hint():
    deadlines = []

    def handle(r):
        deadlines.append(r.extensions["timeout"]["read"])
        return httpx.Response(200, json={"ret": 0, "longpolling_timeout_ms": 55000})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = ILinkClient(http, token="secret")
        await client.updates("")
        await client.updates("next")
    assert deadlines == [40, 60]


async def test_qr_login_verification_and_private_round_trip(tmp_path, monkeypatch):
    requests, qr_codes = [], []
    statuses = iter([
        {"status": "need_verifycode"},
        {"status": "confirmed", "bot_token": "secret", "ilink_bot_id": "bot",
         "ilink_user_id": "owner", "baseurl": API_URL},
    ])

    def handle(request):
        requests.append(request)
        if request.url.path.endswith("get_bot_qrcode"):
            assert request.method == "POST"
            return httpx.Response(200, json={"qrcode": "opaque+&id", "qrcode_img_content": "qr-url"})
        assert request.url.params["qrcode"] == "opaque+&id"
        if len(requests) == 3:
            assert request.url.params["verify_code"] == "123456"
        return httpx.Response(200, json=next(statuses))

    async def code():
        return "123456"

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await auth.login(http, show_qr=qr_codes.append, read_code=code)
    assert qr_codes == ["qr-url"]
    assert result.owner_id == "owner"
    assert all("authorization" not in r.headers for r in requests)
    assert all("base_info" not in json.loads(r.content) for r in requests if r.content)
    root = tmp_path / "private"
    auth.save_credentials(root, result)
    assert auth.load_credentials(root) == result
    assert os.stat(root).st_mode & 0o777 == 0o700
    assert os.stat(root / "credentials.json").st_mode & 0o777 == 0o600
    assert "secret" not in repr(result)


async def test_login_rejects_owner_change_and_unsafe_redirect(credentials):
    for status, code in [
        ({"status": "confirmed", "bot_token": "x", "ilink_bot_id": "b",
          "ilink_user_id": "someone-else"}, "OWNER_CHANGED"),
        ({"status": "scaned_but_redirect", "redirect_host": "evil.example"}, "UNTRUSTED_URL"),
        ({"status": "confirmed", "bot_token": "x", "ilink_bot_id": "b"}, "LOGIN_INCOMPLETE"),
    ]:
        def handle(r):
            return httpx.Response(200, json=(
                {"qrcode": "id", "qrcode_img_content": "url"}
                if r.url.path.endswith("get_bot_qrcode") else status
            ))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            with pytest.raises(WeixinError, match=code):
                await auth.login(http, show_qr=lambda _: None, previous=credentials)


def test_credentials_refuse_symlinks_and_corruption(tmp_path, credentials):
    root = tmp_path / "private"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("unchanged")
    (root / "credentials.json").symlink_to(outside)
    with pytest.raises(WeixinError):
        auth.save_credentials(root, credentials)
    with pytest.raises(WeixinError):
        auth.load_credentials(root)
    assert outside.read_text() == "unchanged"
    (root / "credentials.json").unlink()
    (root / "credentials.json").write_text("invalid")
    with pytest.raises(WeixinError, match="INVALID"):
        auth.load_credentials(root)


@pytest.mark.parametrize("raw_key", [True, False])
async def test_file_download_decrypts_both_key_formats_and_confines_names(tmp_path, raw_key):
    key, content = b"0123456789abcdef", "你好，文件".encode()
    encoded = base64.b64encode(key if raw_key else key.hex().encode()).decode()
    item = {"type": 4, "file_item": {"file_name": "../../secret.txt", "len": str(len(content)),
            "media": {"aes_key": encoded, "encrypt_query_param": "opaque+&param"}}}

    def handle(r):
        assert "authorization" not in r.headers
        assert r.url.params["encrypted_query_param"] == "opaque+&param"
        return httpx.Response(200, content=media.encrypt(content, key))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        path = await media.download(ILinkClient(http, token="secret"), item, tmp_path / "files")
    assert path == tmp_path / "files" / "secret.txt"
    assert path.read_bytes() == content
    assert path.stat().st_mode & 0o777 == 0o600


async def test_file_upload_uses_cdn_without_bearer_and_matches_ciphertext(tmp_path):
    path = tmp_path / "report.html"
    content = b"<html>report</html>"
    path.write_bytes(content)
    metadata = {}

    def handle(r):
        if r.url.path.endswith("getuploadurl"):
            metadata.update(json.loads(r.content))
            assert metadata["media_type"] == 3
            assert metadata["rawsize"] == len(content)
            assert r.headers["authorization"] == "Bearer secret"
            return httpx.Response(200, json={"ret": 0, "upload_param": "upload+&"})
        assert "authorization" not in r.headers
        assert r.url.params["encrypted_query_param"] == "upload+&"
        key = base64.b64encode(bytes.fromhex(metadata["aeskey"])).decode()
        assert media.decrypt(r.content, key) == content
        assert len(r.content) == metadata["filesize"]
        return httpx.Response(200, headers={"x-encrypted-param": "download-ref"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        item = await media.upload(ILinkClient(http, token="secret"), path, "owner",
                                  root=tmp_path, name=path.name)
    assert item["file_item"]["media"]["encrypt_query_param"] == "download-ref"
    assert item["file_item"]["file_name"] == "report.html"


@pytest.mark.parametrize("problem", ["invalid_key", "invalid_padding", "oversized", "external_url", "size_mismatch"])
async def test_file_download_rejects_invalid_inputs(tmp_path, problem):
    key = b"0123456789abcdef"
    item = {"file_item": {"len": "1", "media": {
        "aes_key": base64.b64encode(key).decode(), "encrypt_query_param": "p",
    }}}
    content = media.encrypt(b"x", key)
    if problem == "invalid_key":
        item["file_item"]["media"]["aes_key"] = "bad base64"
    if problem == "invalid_padding":
        content = b"broken"
    if problem == "oversized":
        item["file_item"]["len"] = str(MAX_FILE_BYTES + 1)
    if problem == "external_url":
        item["file_item"]["media"]["full_url"] = "https://evil.example/file"
    if problem == "size_mismatch":
        item["file_item"]["len"] = "2"
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=content)
    )) as http:
        with pytest.raises(WeixinError):
            await media.download(ILinkClient(http), item, tmp_path)
    assert not list(tmp_path.iterdir())


def test_local_file_bounds_and_symlink_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "private"
    outside.write_text("secret")
    (root / "link").symlink_to(outside)
    with pytest.raises(WeixinError, match="OUTSIDE_ROOT"):
        media.read_file(root / "link", permitted_root=root)
    large = root / "large"
    with large.open("wb") as stream:
        stream.truncate(MAX_FILE_BYTES + 1)
    with pytest.raises(WeixinError, match="TOO_LARGE"):
        media.read_file(large, permitted_root=root)
    fifo = root / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(WeixinError, match="NOT_REGULAR"):
        media.read_file(fifo, permitted_root=root)


def test_weixin_transport_secrets_are_redacted():
    from karen.privacy import redact

    result = redact({k: "secret-value" for k in (
        "bot_token", "context_token", "typing_ticket", "aes_key", "aeskey",
        "encrypt_query_param", "encrypted_query_param",
    )})
    assert set(result.values()) == {"[REDACTED]"}
