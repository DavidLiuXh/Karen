"""Tencent iLink personal Weixin channel; no OpenClaw runtime is required."""

from .api import ILinkClient, WeixinError
from .auth import load_credentials, login, save_credentials
from .media import prepare_file_tool
from .service import WeixinChannel
from .storage import WeixinStore

__all__ = [
    "ILinkClient", "WeixinChannel", "WeixinError", "WeixinStore", "load_credentials",
    "login", "prepare_file_tool", "save_credentials",
]
