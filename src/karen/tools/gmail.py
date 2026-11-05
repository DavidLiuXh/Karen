"""Read-only Gmail API access; callers own credentials, HTTP client and cursors."""

import asyncio
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
_API = "https://gmail.googleapis.com/gmail/v1/users/me"
_TOKEN_URL = "https://oauth2.googleapis.com/token"


class GmailError(Exception):
    """Safe error code without credentials or private response contents."""

    def __init__(self, code: str, *, status_code: int | None = None):
        super().__init__(code)
        self.status_code = status_code


@dataclass(frozen=True)
class GmailCredentials:
    client_id: str
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)


class GmailClient:
    """Async client using an existing OAuth grant with gmail.readonly scope.

    Methods return Gmail JSON unchanged. Page tokens and history IDs are opaque
    strings. The caller persists a history cursor only after processing all pages.
    This client neither marks messages read nor stores tokens on disk.
    """

    def __init__(self, http: httpx.AsyncClient, credentials: GmailCredentials):
        self._http = http
        self._credentials = credentials
        self._access_token: str | None = None
        self._refresh_lock = asyncio.Lock()

    async def _refresh(self, previous: str | None) -> None:
        async with self._refresh_lock:
            if self._access_token != previous:
                return
            response = await self._send("POST", _TOKEN_URL, data={
                "client_id": self._credentials.client_id,
                "client_secret": self._credentials.client_secret,
                "refresh_token": self._credentials.refresh_token,
                "grant_type": "refresh_token",
            })
            payload = self._json(response, "GMAIL_AUTH_FAILED")
            token = payload.get("access_token")
            if not isinstance(token, str) or not token:
                raise GmailError("GMAIL_RESPONSE_INVALID")
            self._access_token = token

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._http.request(
                method, url, follow_redirects=False, timeout=30, **kwargs
            )
        except httpx.RequestError:
            raise GmailError("GMAIL_TRANSPORT_FAILED") from None

    @staticmethod
    def _json(response: httpx.Response, code: str) -> dict[str, Any]:
        if not response.is_success:
            raise GmailError(code, status_code=response.status_code)
        try:
            payload = response.json()
        except ValueError:
            raise GmailError("GMAIL_RESPONSE_INVALID") from None
        if not isinstance(payload, dict):
            raise GmailError("GMAIL_RESPONSE_INVALID")
        return payload

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._access_token is None:
            await self._refresh(None)
        token = self._access_token
        response = await self._send(
            "GET", _API + path, params=params, headers={"Authorization": f"Bearer {token}"}
        )
        if response.status_code == 401:
            await self._refresh(token)
            response = await self._send(
                "GET", _API + path, params=params,
                headers={"Authorization": f"Bearer {self._access_token}"},
            )
        code = "GMAIL_HISTORY_EXPIRED" if path == "/history" and response.status_code == 404 \
            else "GMAIL_API_FAILED"
        return self._json(response, code)

    async def get_profile(self) -> dict[str, Any]:
        """Return mailbox profile, including the current historyId baseline."""
        return await self._get("/profile")

    async def list_inbox(
        self, *, page_token: str | None = None, max_results: int = 100
    ) -> dict[str, Any]:
        """Return one page of INBOX message IDs (read and unread)."""
        if not 1 <= max_results <= 500:
            raise ValueError("max_results must be between 1 and 500")
        params: dict[str, Any] = {"labelIds": "INBOX", "maxResults": max_results}
        if page_token:
            params["pageToken"] = page_token
        return await self._get("/messages", params)

    async def get_message(self, message_id: str) -> dict[str, Any]:
        """Return full message, including MIME payload and base64url body data."""
        if not message_id:
            raise ValueError("message_id must not be empty")
        return await self._get("/messages/" + quote(message_id, safe=""), {"format": "full"})

    async def list_history(
        self, start_history_id: str, *, page_token: str | None = None
    ) -> dict[str, Any]:
        """Return one page of mailbox changes since a baseline.

        No label filter: consumers inspect messageAdded/labelAdded events to
        distinguish incoming INBOX messages from other changes. An expired cursor
        raises GMAIL_HISTORY_EXPIRED; callers must resync rather than assume no mail.
        """
        if not start_history_id:
            raise ValueError("start_history_id must not be empty")
        params = {"startHistoryId": start_history_id}
        if page_token:
            params["pageToken"] = page_token
        return await self._get("/history", params)
