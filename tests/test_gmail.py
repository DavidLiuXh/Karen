import httpx
import pytest

from karen.tools.gmail import GmailClient, GmailCredentials, GmailError

CREDS = GmailCredentials("client", "private-secret", "private-refresh")


async def test_inbox_pages_full_message_and_history():
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "private-access"})
        assert request.headers["authorization"] == "Bearer private-access"
        return httpx.Response(200, json={"nextPageToken": "next", "historyId": "123"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = GmailClient(http, CREDS)
        await client.get_profile()
        result = await client.list_inbox(page_token="page", max_results=500)
        await client.get_message("message-id")
        await client.list_history("123", page_token="history-page")
    assert result["nextPageToken"] == "next"
    assert len(requests) == 5
    assert requests[2].url.params == httpx.QueryParams(
        {"labelIds": "INBOX", "maxResults": 500, "pageToken": "page"}
    )
    assert requests[3].url.params["format"] == "full"
    assert requests[4].url.params["startHistoryId"] == "123"
    assert "private" not in repr(CREDS)


async def test_unauthorized_refreshes_once():
    refreshes = 0
    calls = 0

    def handle(request):
        nonlocal refreshes, calls
        if request.url.host == "oauth2.googleapis.com":
            refreshes += 1
            return httpx.Response(200, json={"access_token": f"token-{refreshes}"})
        calls += 1
        return httpx.Response(401, json={"private": "mail"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(GmailError, match="GMAIL_API_FAILED") as error:
            await GmailClient(http, CREDS).get_profile()
    assert refreshes == calls == 2
    assert error.value.status_code == 401
    assert "private" not in str(error.value)


@pytest.mark.parametrize("status,payload,code", [
    (404, {}, "GMAIL_HISTORY_EXPIRED"),
    (429, {"error": "private"}, "GMAIL_API_FAILED"),
    (200, [], "GMAIL_RESPONSE_INVALID"),
    (302, {}, "GMAIL_API_FAILED"),
])
async def test_history_errors(status, payload, code):
    def handle(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "token"})
        return httpx.Response(status, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(GmailError, match=code):
            await GmailClient(http, CREDS).list_history("123")


async def test_validation_does_not_access_network():
    async with httpx.AsyncClient() as http:
        client = GmailClient(http, CREDS)
        with pytest.raises(ValueError):
            await client.list_inbox(max_results=501)
        with pytest.raises(ValueError):
            await client.get_message("")
        with pytest.raises(ValueError):
            await client.list_history("")


async def test_refresh_failure_is_safe():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"error_description": "private-refresh"})
    )) as http:
        with pytest.raises(GmailError, match="GMAIL_AUTH_FAILED") as error:
            await GmailClient(http, CREDS).get_profile()
    assert "private" not in str(error.value)
