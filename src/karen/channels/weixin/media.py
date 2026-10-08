"""Bounded file transfer and the explicit task artifact handoff to Weixin."""

import asyncio
import base64
import hashlib
import os
import secrets
import stat
import tempfile
from pathlib import Path
from urllib.parse import urlencode

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from dynamic_graph.capabilities.registry import ToolDefinition
from dynamic_graph.execution.errors import ToolCallError

from .api import CDN_URL, MAX_FILE_BYTES, WeixinError


def encrypt(content, key):
    padder = padding.PKCS7(128).padder()
    padded = padder.update(content) + padder.finalize()
    encoder = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encoder.update(padded) + encoder.finalize()


def decrypt(content, encoded_key):
    try:
        key = base64.b64decode(encoded_key, validate=True)
        if len(key) == 32:
            key = bytes.fromhex(key.decode("ascii"))
        if len(key) != 16:
            raise ValueError
        decoder = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        padded = decoder.update(content) + decoder.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except (ValueError, TypeError, UnicodeError):
        raise WeixinError("WEIXIN_FILE_INVALID") from None


def read_file(path, *, permitted_root):
    """Resolve containment, then refuse symlinks at open and enforce a streaming limit."""
    try:
        path = Path(path).resolve(strict=True)
        root = Path(permitted_root).resolve(strict=True)
        if not path.is_relative_to(root) or path == root:
            raise WeixinError("WEIXIN_FILE_OUTSIDE_ROOT")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise WeixinError("WEIXIN_FILE_NOT_REGULAR")
            content = handle.read(MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            raise WeixinError("WEIXIN_FILE_TOO_LARGE")
        return content, path.name
    except (OSError, ValueError, RuntimeError):
        raise WeixinError("WEIXIN_FILE_UNREADABLE") from None


def safe_name(name):
    name = str(name or "attachment.bin").replace("\\", "/").split("/")[-1]
    name = "".join(c for c in name if c.isprintable() and c not in {"/", "\\"})
    return name[:120] if name not in {"", ".", ".."} else "attachment.bin"


def save_file(directory, content, name):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".file-", dir=directory)
    target = directory / safe_name(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target


async def download(client, item, directory):
    try:
        file = item["file_item"]
        if not isinstance(file, dict):
            raise ValueError
        media = file["media"]
        if not isinstance(media, dict):
            raise ValueError
        if media.get("encrypt_type", 1) != 1:
            raise ValueError
        length = int(file["len"]) if "len" in file else None
        if length is not None and not 0 <= length <= MAX_FILE_BYTES:
            raise WeixinError("WEIXIN_FILE_TOO_LARGE")
        url = media.get("full_url") or f"{CDN_URL}/download?" + urlencode({
            "encrypted_query_param": media["encrypt_query_param"],
        })
        ciphertext, _ = await client.transfer("GET", url, limit=MAX_FILE_BYTES + 16, timeout=60)
        content = decrypt(ciphertext, media["aes_key"])
        if len(content) > MAX_FILE_BYTES or (length is not None and len(content) != length):
            raise WeixinError("WEIXIN_FILE_SIZE_MISMATCH")
        return await asyncio.to_thread(save_file, directory, content, file.get("file_name"))
    except (KeyError, TypeError, ValueError):
        raise WeixinError("WEIXIN_FILE_INVALID") from None


async def upload(client, path, peer, *, root, name):
    content, _ = await asyncio.to_thread(read_file, path, permitted_root=root)
    key, filekey = secrets.token_bytes(16), secrets.token_hex(16)
    ciphertext = encrypt(content, key)
    result = await client.request("ilink/bot/getuploadurl", body={
        "filekey": filekey, "media_type": 3, "to_user_id": peer,
        "rawsize": len(content), "rawfilemd5": hashlib.md5(content).hexdigest(),
        "filesize": len(ciphertext), "no_need_thumb": True, "aeskey": key.hex(),
    })
    url = result.get("upload_full_url")
    if not url:
        if not result.get("upload_param"):
            raise WeixinError("WEIXIN_UPLOAD_URL_MISSING")
        url = f"{CDN_URL}/upload?" + urlencode({
            "encrypted_query_param": result["upload_param"], "filekey": filekey,
        })
    _, headers = await client.transfer("POST", url, content=ciphertext,
                                       headers={"Content-Type": "application/octet-stream"},
                                       timeout=60)
    download_param = headers.get("x-encrypted-param")
    if not download_param:
        raise WeixinError("WEIXIN_UPLOAD_UNCONFIRMED", retryable=True)
    return {"type": 4, "file_item": {"file_name": safe_name(name), "len": str(len(content)),
            "media": {"encrypt_query_param": download_param, "encrypt_type": 1,
                      "aes_key": base64.b64encode(key.hex().encode()).decode()}}}


def prepare_file_tool(store):
    async def prepare(data, context):
        if context.cancellation_token.cancelled:
            raise asyncio.CancelledError
        try:
            content, name = await asyncio.to_thread(read_file, data["path"], permitted_root="/tmp")
            # A stable snapshot survives source edits and transport retries.
            artifact_id = hashlib.sha256(
                context.run_id.encode() + b"\0" + name.encode() + b"\0" + content
            ).hexdigest()
            target = await asyncio.to_thread(
                save_file, store.root / "artifacts" / artifact_id, content, name,
            )
            store.prepare_file(context.run_id, artifact_id, str(target), name)
            return {"attachment_id": artifact_id, "prepared": True, "bytes": len(content)}
        except WeixinError as exc:
            raise ToolCallError(exc.code) from None

    return ToolDefinition(
        "weixin.prepare_file", "1.0.0",
        "Prepare a local file under /tmp (up to 10 MiB) for delivery to the authenticated "
        "Weixin owner AFTER this task completes. Call for files the user requests to receive "
        "in Weixin, after their producer. Success means queued preparation, not delivery. "
        "Do not use for CLI requests or files unrelated to the user's request.",
        {"type": "object", "properties": {"path": {"type": "string"}},
         "required": ["path"], "additionalProperties": False},
        {"type": "object", "properties": {"attachment_id": {"type": "string"},
         "prepared": {"type": "boolean"}, "bytes": {"type": "integer"}},
         "required": ["attachment_id", "prepared", "bytes"], "additionalProperties": False},
        prepare, read_only=False, idempotent=True,
    )
