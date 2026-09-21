# ruff: noqa: PLR0917, PLR2004

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import pytest
import pytest_asyncio
from aioresponses import aioresponses

from custom_components.securecontrols_thermostat.api import (
    WS_SUBPROTOCOL,
    WS_URL,
    ApiError,
    CannotConnect,
    InvalidAuth,
    SecureControlsClient,
    ServerRejected,
    Thermostat,
    _encode_password,
)


@pytest_asyncio.fixture
async def session():
    async with aiohttp.ClientSession() as client_session:
        yield client_session


class _WSMsg:
    def __init__(self, data: str = "", msg_type: aiohttp.WSMsgType = aiohttp.WSMsgType.TEXT):
        self.type = msg_type
        self.data = data


class FakeWS:
    """Small request/response WebSocket stand-in."""

    def __init__(
        self,
        incoming: list[dict[str, Any] | _WSMsg] | None = None,
        receive_gate: asyncio.Event | None = None,
    ) -> None:
        self._incoming = list(incoming or [])
        self._receive_gate = receive_gate
        self._closed_event = asyncio.Event()
        self.sent: list[Any] = []
        self.closed = False

    async def send_json(self, payload: Any, *, dumps=json.dumps) -> None:
        self.sent.append(payload)
        self.sent_text = getattr(self, "sent_text", []) + [dumps(payload)]

    async def receive(self) -> _WSMsg:
        if self._receive_gate is not None:
            await self._receive_gate.wait()
        if self._incoming:
            item = self._incoming.pop(0)
            if isinstance(item, _WSMsg):
                return item
            return _WSMsg(json.dumps(item))
        await self._closed_event.wait()
        return _WSMsg(msg_type=aiohttp.WSMsgType.CLOSED)

    async def close(self) -> None:
        self.closed = True
        self._closed_event.set()


def _authenticated_client(session: aiohttp.ClientSession) -> SecureControlsClient:
    client = SecureControlsClient(session)
    client._jwt = "jwt"
    client._session_id = 42
    client.thermostat = Thermostat(gmi="1001", sn="SN", hn="HN")
    return client


def _patch_websockets(monkeypatch, websockets: list[FakeWS]):
    calls: list[dict[str, Any]] = []

    async def fake_ws_connect(_session, url, **kwargs):
        calls.append({"url": url, **kwargs})
        return websockets[len(calls) - 1]

    monkeypatch.setattr(aiohttp.ClientSession, "ws_connect", fake_ws_connect)
    return calls


def test_encode_password_md5_lower_hex_ok():
    digest = _encode_password("secret")
    assert digest == "5ebe2294ecd0e0f08eab7690d2a6ee69"
    assert len(digest) == 32
    assert digest == digest.lower()


@pytest.mark.asyncio
async def test_login_success_clears_auth_latch(session):
    client = SecureControlsClient(session)
    client._auth_rejected = True
    ok_body = {
        "RI": 0,
        "D": {
            "JT": "jwt-token-123",
            "SI": 777,
            "JTT": 123456,
            "UI": 42,
            "GD": [
                {"GMI": 1001, "SN": "ABC", "HN": "Thermo-1", "CS": 1, "UR": 100},
            ],
        },
    }

    with aioresponses() as mocked:
        mocked.post(
            "https://app.beanbag.online/api/UserRestAPI/LoginRequest",
            payload=ok_body,
            status=200,
        )
        await client.login("user@example.com", "secret")

    assert client._jwt == "jwt-token-123"
    assert client._session_id == 777
    assert client._user_id == 42
    assert client._auth_rejected is False
    assert isinstance(client.thermostat, Thermostat)
    assert client.thermostat.gmi == "1001"
    assert client.thermostat.sn == "ABC"


@pytest.mark.asyncio
async def test_login_unauthorized_status_latches_auth(session):
    client = SecureControlsClient(session)
    with aioresponses() as mocked:
        mocked.post(
            "https://app.beanbag.online/api/UserRestAPI/LoginRequest",
            status=401,
            payload={"D": {}},
        )
        with pytest.raises(InvalidAuth):
            await client.login("user@example.com", "secret")

    assert client._auth_rejected is True


@pytest.mark.asyncio
async def test_login_server_error_raises_cannot_connect_without_latching(session):
    client = SecureControlsClient(session)
    with aioresponses() as mocked:
        mocked.post(
            "https://app.beanbag.online/api/UserRestAPI/LoginRequest",
            status=500,
            payload={"D": {}},
        )
        with pytest.raises(CannotConnect):
            await client.login("user@example.com", "secret")

    assert client._auth_rejected is False


@pytest.mark.asyncio
async def test_login_bad_json_raises_cannot_connect(session):
    client = SecureControlsClient(session)
    with aioresponses() as mocked:
        mocked.post(
            "https://app.beanbag.online/api/UserRestAPI/LoginRequest",
            body="not-json",
            status=200,
            headers={"Content-Type": "text/plain"},
        )
        with pytest.raises(CannotConnect):
            await client.login("user@example.com", "secret")


@pytest.mark.asyncio
async def test_login_missing_jt_si_raises_invalid_auth_without_latching(session):
    client = SecureControlsClient(session)
    broken = {
        "RI": -1,
        "D": {"GD": [{"GMI": 1001, "SN": "ABC", "HN": "Thermo-1"}]},
    }
    with aioresponses() as mocked:
        mocked.post(
            "https://app.beanbag.online/api/UserRestAPI/LoginRequest",
            payload=broken,
            status=200,
        )
        with pytest.raises(InvalidAuth):
            await client.login("user@example.com", "secret")

    assert client._auth_rejected is False


@pytest.mark.asyncio
async def test_login_no_devices_raises_api_error(session):
    client = SecureControlsClient(session)
    with aioresponses() as mocked:
        mocked.post(
            "https://app.beanbag.online/api/UserRestAPI/LoginRequest",
            payload={"RI": 0, "D": {"JT": "jwt", "SI": 9, "GD": []}},
            status=200,
        )
        with pytest.raises(ApiError):
            await client.login("user@example.com", "secret")


@pytest.mark.asyncio
async def test_request_uses_expected_headers_correlates_and_stays_open(session, monkeypatch):
    client = _authenticated_client(session)
    monkeypatch.setattr(SecureControlsClient, "_now_epoch", staticmethod(lambda: 1700000000))
    monkeypatch.setattr(SecureControlsClient, "_new_corr", lambda self: "42-deadbeef")
    websocket = FakeWS([{"I": "42-deadbeef", "R": {"ok": 1}}])
    calls = _patch_websockets(monkeypatch, [websocket])

    result = await client.device_metadata_read()

    assert result == {"ok": 1}
    assert websocket.closed is False
    assert client._ws is websocket
    assert calls[0]["url"] == WS_URL
    assert calls[0]["headers"]["Authorization"] == "Bearer jwt"
    assert calls[0]["headers"]["Session-id"] == "42"
    assert WS_SUBPROTOCOL in calls[0]["protocols"]
    sent = websocket.sent[0]
    assert sent["M"] == "Request"
    assert sent["I"] == "42-deadbeef"
    assert sent["DTS"] == 1700000000
    assert sent["P"][0] == {"GMI": 1001, "HI": 17, "SI": 11}


@pytest.mark.asyncio
async def test_two_polls_reuse_one_login_and_one_websocket(session, monkeypatch):
    client = _authenticated_client(session)
    correlations = iter(("42-first", "42-second"))
    monkeypatch.setattr(SecureControlsClient, "_new_corr", lambda self: next(correlations))
    websocket = FakeWS(
        [
            {"I": "42-first", "R": {"poll": 1}},
            {"I": "42-second", "R": {"poll": 2}},
        ]
    )
    calls = _patch_websockets(monkeypatch, [websocket])

    assert await client.state_read() == {"poll": 1}
    assert await client.state_read() == {"poll": 2}

    assert len(calls) == 1
    assert websocket.closed is False
    assert client._ws is websocket


@pytest.mark.asyncio
async def test_request_ignores_unrelated_notify_and_non_json_frames(session, monkeypatch):
    client = _authenticated_client(session)
    monkeypatch.setattr(SecureControlsClient, "_new_corr", lambda self: "42-wanted")
    websocket = FakeWS(
        [
            _WSMsg("not-json"),
            {"M": "Notify", "P": []},
            {"I": "42-other", "R": {"wrong": True}},
            {"I": "42-wanted", "R": {"ok": True}},
        ]
    )
    _patch_websockets(monkeypatch, [websocket])

    assert await client.state_read() == {"ok": True}
    assert websocket.closed is False


@pytest.mark.asyncio
async def test_disconnect_closes_established_socket(session, monkeypatch):
    client = _authenticated_client(session)
    monkeypatch.setattr(SecureControlsClient, "_new_corr", lambda self: "42-ok")
    websocket = FakeWS([{"I": "42-ok", "R": {"ok": True}}])
    _patch_websockets(monkeypatch, [websocket])

    assert await client.state_read() == {"ok": True}
    assert websocket.closed is False

    await client.disconnect()

    assert websocket.closed is True
    assert client._ws is None


@pytest.mark.asyncio
async def test_server_rejection_closes_socket(session, monkeypatch):
    client = _authenticated_client(session)
    monkeypatch.setattr(SecureControlsClient, "_new_corr", lambda self: "42-error")
    websocket = FakeWS([{"I": "42-error", "E": {"C": 500, "EC": 9, "EA": {}}}])
    _patch_websockets(monkeypatch, [websocket])

    with pytest.raises(ServerRejected):
        await client.state_read()

    assert websocket.closed is True
    assert client._auth_rejected is False


@pytest.mark.asyncio
async def test_auth_rejection_latches_and_stops_network_activity(session, monkeypatch):
    client = _authenticated_client(session)
    monkeypatch.setattr(SecureControlsClient, "_new_corr", lambda self: "42-auth")
    websocket = FakeWS([{"I": "42-auth", "E": {"C": 203, "EC": 2, "EA": {}}}])
    calls = _patch_websockets(monkeypatch, [websocket])

    with pytest.raises(InvalidAuth):
        await client.state_read()
    with pytest.raises(InvalidAuth):
        await client.state_read()

    assert websocket.closed is True
    assert client._auth_rejected is True
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_lost_established_session_reconnect_rejection_latches(session, monkeypatch):
    client = _authenticated_client(session)
    monkeypatch.setattr(SecureControlsClient, "_new_corr", lambda self: "42-ok")
    established = FakeWS([{"I": "42-ok", "R": {"ok": True}}])
    attempts = 0

    async def fake_ws_connect(_session, _url, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return established
        raise aiohttp.WSServerHandshakeError(None, (), status=403, message="Forbidden")

    monkeypatch.setattr(aiohttp.ClientSession, "ws_connect", fake_ws_connect)

    assert await client.state_read() == {"ok": True}
    assert attempts == 1

    # The established session drops unexpectedly (e.g. the mobile app reclaimed it).
    established._incoming.append(_WSMsg(msg_type=aiohttp.WSMsgType.CLOSED))
    with pytest.raises(CannotConnect):
        await client.state_read()
    assert attempts == 1  # reused the existing socket; no reconnect attempted here

    # Beanbag refuses a second socket on the same session: latch, don't keep retrying.
    with pytest.raises(InvalidAuth):
        await client.state_read()
    with pytest.raises(InvalidAuth):
        await client.state_read()

    assert client._auth_rejected is True
    assert attempts == 2


@pytest.mark.asyncio
async def test_unauthorized_handshake_latches_and_stops_network_activity(session, monkeypatch):
    client = _authenticated_client(session)
    attempts = 0

    async def fake_ws_connect(_session, _url, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise aiohttp.WSServerHandshakeError(
            None,
            (),
            status=403,
            message="Forbidden",
        )

    monkeypatch.setattr(aiohttp.ClientSession, "ws_connect", fake_ws_connect)

    with pytest.raises(InvalidAuth):
        await client.state_read()
    with pytest.raises(InvalidAuth):
        await client.state_read()

    assert client._auth_rejected is True
    assert attempts == 1


@pytest.mark.asyncio
async def test_transport_failure_does_not_login_or_latch_and_next_poll_can_retry(
    session, monkeypatch
):
    client = _authenticated_client(session)
    client.login = AsyncMock()
    websocket = FakeWS([{"I": "42-ok", "R": {"ok": True}}])
    attempts = 0

    async def fake_ws_connect(_session, _url, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise aiohttp.ClientConnectionError("offline")
        return websocket

    monkeypatch.setattr(aiohttp.ClientSession, "ws_connect", fake_ws_connect)
    monkeypatch.setattr(SecureControlsClient, "_new_corr", lambda self: "42-ok")

    with pytest.raises(CannotConnect):
        await client.state_read()
    assert await client.state_read() == {"ok": True}

    client.login.assert_not_awaited()
    assert client._auth_rejected is False
    assert attempts == 2
    assert websocket.closed is False
    assert client._ws is websocket


@pytest.mark.asyncio
async def test_send_transport_failure_closes_socket(session, monkeypatch):
    client = _authenticated_client(session)
    websocket = FakeWS()
    websocket.send_json = AsyncMock(side_effect=aiohttp.ClientConnectionError("disconnected"))
    _patch_websockets(monkeypatch, [websocket])

    with pytest.raises(CannotConnect, match="request failed"):
        await client.state_read()

    assert websocket.closed is True
    assert client._auth_rejected is False


@pytest.mark.asyncio
async def test_response_timeout_closes_socket(session, monkeypatch):
    client = _authenticated_client(session)
    websocket = FakeWS()
    _patch_websockets(monkeypatch, [websocket])
    monkeypatch.setattr(
        "custom_components.securecontrols_thermostat.api.WS_RESPONSE_TIMEOUT_SECS", 0.01
    )

    with pytest.raises(CannotConnect, match="timed out"):
        await client.state_read()

    assert websocket.closed is True
    assert client._ws is None


@pytest.mark.asyncio
async def test_request_cancellation_closes_socket(session, monkeypatch):
    client = _authenticated_client(session)
    websocket = FakeWS()
    _patch_websockets(monkeypatch, [websocket])

    task = asyncio.create_task(client.state_read())
    while not websocket.sent:
        await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert websocket.closed is True
    assert client._ws is None


@pytest.mark.asyncio
async def test_concurrent_requests_are_serialized_on_one_connection(session, monkeypatch):
    client = _authenticated_client(session)
    correlations = iter(("42-first", "42-second"))
    monkeypatch.setattr(SecureControlsClient, "_new_corr", lambda self: next(correlations))
    release_first = asyncio.Event()
    websocket = FakeWS(
        [
            {"I": "42-first", "R": {"request": 1}},
            {"I": "42-second", "R": {"request": 2}},
        ],
        receive_gate=release_first,
    )
    calls = _patch_websockets(monkeypatch, [websocket])

    first_task = asyncio.create_task(client.state_read())
    while not websocket.sent:
        await asyncio.sleep(0)
    second_task = asyncio.create_task(client.device_config_read())
    await asyncio.sleep(0)

    assert len(calls) == 1
    assert len(websocket.sent) == 1  # second request waits on the lock, not a new socket

    release_first.set()
    assert await first_task == {"request": 1}
    assert await second_task == {"request": 2}

    assert len(calls) == 1
    assert len(websocket.sent) == 2
    assert websocket.closed is False
    assert client._ws is websocket


@pytest.mark.asyncio
async def test_closed_socket_before_response_raises_cannot_connect(session, monkeypatch):
    client = _authenticated_client(session)
    websocket = FakeWS([_WSMsg(msg_type=aiohttp.WSMsgType.CLOSED)])
    _patch_websockets(monkeypatch, [websocket])

    with pytest.raises(CannotConnect, match="closed"):
        await client.state_read()
    assert websocket.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "expected_item", "expected_value", "expected_ot", "expected_duration"),
    [
        ("target", 1, 215, 1, 0),
        ("mode", 3, 1, 1, 0),
        ("hold", 1, 190, 2, 45),
    ],
)
async def test_write_payloads(
    session,
    monkeypatch,
    method_name,
    expected_item,
    expected_value,
    expected_ot,
    expected_duration,
):
    client = _authenticated_client(session)
    monkeypatch.setattr(SecureControlsClient, "_new_corr", lambda self: "42-write")
    websocket = FakeWS([{"I": "42-write", "R": {"ok": True}}])
    _patch_websockets(monkeypatch, [websocket])

    if method_name == "target":
        await client.set_target_temp(21.5)
    elif method_name == "mode":
        await client.set_mode(True)
    else:
        await client.set_timed_hold(19.0, 45)

    sent = websocket.sent[0]
    assert sent["P"][0]["HI"] == 2
    assert sent["P"][0]["SI"] == 15
    assert sent["P"][1][0] == 1
    body = sent["P"][1][1]
    assert body == {
        "I": expected_item,
        "V": expected_value,
        "OT": expected_ot,
        "D": expected_duration,
    }
    assert websocket.closed is False
