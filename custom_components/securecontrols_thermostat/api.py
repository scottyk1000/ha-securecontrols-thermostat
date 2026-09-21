from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

# --------------------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------------------
_LOGGER = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# Types / constants
# --------------------------------------------------------------------------------------
Json = dict[str, Any]

WS_URL = "wss://app.beanbag.online/api/TransactionRestAPI/ConnectWebSocket"
WS_SUBPROTOCOL = "BB-BO-01"
WS_RESPONSE_TIMEOUT_SECS = 15
# Schedule writes are relayed to the programmer; allow a little longer than other requests.
PROGRAM_WRITE_TIMEOUT_SECS = 60
# Programmers (H3747 confirmed) silently drop requests larger than ~1 KB. A weekly schedule
# is 898 bytes as compact JSON but 1083 bytes with json.dumps' default ", " / ": "
# separators, so every request is sent compact, exactly like the official app.
MAX_REQUEST_BYTES = 1024


def _compact_dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))
PASSWORD_DIGEST_LENGTH = 32
HTTP_OK = 200
HTTP_SERVER_ERROR = 500
AUTH_ERROR_CODE = 203
AUTH_ERROR_SUBCODE = 2

# Thermostat "block" constants (per your usage)
THERMO_HI_WRITE = 2  # thermostat.state.write
THERMO_SI = 15  # thermostat state block
THERMO_SLOT = 1  # slot used in your integration

# Item map (SI:15, slot 1) — updated
ITEM_TARGET = 1  # target_c (deci °C)
ITEM_AMBIENT = 2  # ambient_c (deci °C)
ITEM_HVAC = 3  # hvac: 0=off, 1=heat
ITEM_PRESET = 6  # preset: 1=away, 2=home
ITEM_HUMID = 8  # %RH
ITEM_NEXT_TIME = 9  # next schedule time (mins)
ITEM_NEXT_TARGET = 10  # next scheduled target temp (deci °C)
ITEM_FROST = 11  # frost_c (deci °C)

# Hot water channel (H3747 / C1727): block SI:16, slot = channel number
HOT_WATER_SI = 16
ITEM_HW_BOOST = 4  # V=1 while boosting; OT:2 with D=<minutes> starts, D=0 cancels
ITEM_HW_NEXT_TIME = 9  # next change / boost end (minutes since local midnight)
ITEM_HW_NEXT_STATE = 10  # state after the next change (0=off, 1=on)


# ---- Thermostat metadata (gateway == device) ----
@dataclass
class Thermostat:
    gmi: str
    sn: str
    hn: str
    cs: int | None = None
    ur: int | None = None
    hi: int | None = None
    dt: int | None = None
    dn: str | None = None


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------
class ApiError(Exception):
    pass


class InvalidAuth(ApiError):
    """Authentication rejected; explicit login is required to resume requests."""

    pass


class CannotConnect(ApiError):
    pass


class ServerRejected(ApiError):
    """Application-level error returned by the SecureControls API."""

    def __init__(self, code: int | None, subcode: int | None, details: Any) -> None:
        super().__init__(f"Server rejected request (C={code} EC={subcode}) details={details}")
        self.code = code
        self.subcode = subcode
        self.details = details


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
def _encode_password(pw: str) -> str:
    """
    SecureControls / Beanbag login expects MD5(password).hexdigest()
    (32 lowercase hex characters). Do NOT truncate.
    """
    digest = hashlib.md5(pw.encode("utf-8")).hexdigest()
    if len(digest) != PASSWORD_DIGEST_LENGTH or any(ch not in "0123456789abcdef" for ch in digest):
        raise ValueError("Password digest must be a 32-character lowercase hex string")
    return digest


def _decode_api_error(
    err_obj: dict[str, Any],
) -> tuple[int | None, int | None, dict[str, Any], str]:
    """
    Extract (C, EC, EA, message) from either a WS 'E' object or a REST envelope.
    Accepts shapes like:
      {'C': 203, 'M': '...', 'D': {'ES':203, 'EC':2, 'ET':203, 'EA':{...}}}
      or the WS 'E' object directly: {'C':203,'EC':2,'EA':{...},'M':'...'}
    """
    if not isinstance(err_obj, dict):
        return None, None, {}, f"{err_obj!r}"

    # Top-level fields
    c = err_obj.get("C")
    ec = err_obj.get("EC")
    ea = err_obj.get("EA") or {}
    msg = err_obj.get("M") or ""

    # Nested detail (common on REST envelope)
    d = err_obj.get("D")
    if isinstance(d, dict):
        c = c or d.get("ES") or d.get("ET")
        ec = ec or d.get("EC")
        ea = ea or d.get("EA") or {}
        msg = msg or d.get("M") or ""

    return c, ec, ea, msg or ""


# --------------------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------------------
class SecureControlsClient:
    """
    Secure Controls / Beanbag client
    - HTTP login to get JWT + SessionId + GD
    - One persistent WebSocket, reused for transactions with the BB-BO-01 subprotocol
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._http = session
        self._base = "https://app.beanbag.online"

        # Auth/session
        self._jwt: str | None = None  # D.JT
        self._session_id: int | None = None  # D.SI
        self._session_ts: int | None = None  # D.JTT
        self._user_id: int | None = None  # D.UI

        # Device (gateway == thermostat)
        self.thermostat: Thermostat | None = None
        # Every gateway on the account (raw GD entries) and, optionally, the
        # one the config entry was created for.
        self.gateways: list[dict[str, Any]] = []
        self.preferred_gmi: str | None = None

        # A single WebSocket is opened after login and reused for every poll
        # and command until unload or failure. Beanbag does not reliably
        # permit a second WebSocket on the same session, so a lost connection
        # is only ever reopened with the existing session/JWT; if that is
        # rejected, the auth latch below forces a manual reload (fresh login)
        # rather than a silent reconnect loop.
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._operation_lock = asyncio.Lock()
        self._auth_rejected = False

    # --------------- Utilities ---------------
    @staticmethod
    def _now_epoch() -> int:
        return int(time.time())

    @staticmethod
    def c_to_deci(c: float) -> int:
        return round(c * 10)

    @staticmethod
    def deci_to_c(v: int) -> float:
        return float(v) / 10.0

    def _new_corr(self) -> str:
        sid = str(self._session_id or "0")
        return f"{sid}-{secrets.token_hex(4)}"

    # --------------- HTTP: Login ---------------
    async def login(self, email: str, password: str) -> None:
        payload = {
            "ULC": {
                "OI": 1550005,
                "NT": "SetLogin",
                "UEI": email.strip(),
                "P": _encode_password(password),
            }
        }
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "Request-id": "1",
        }
        try:
            resp = await self._http.post(
                f"{self._base}/api/UserRestAPI/LoginRequest", json=payload, headers=headers
            )
        except aiohttp.ClientError as e:
            _LOGGER.error("SecureControls: HTTP exception during login: %s", e)
            raise CannotConnect(f"HTTP error connecting: {e}") from e

        if resp.status in (401, 403):
            self._auth_rejected = True
            _LOGGER.warning(
                "SecureControls: login HTTP status %s (unauthorized/forbidden)", resp.status
            )
            raise InvalidAuth("HTTP unauthorized/forbidden")
        if resp.status >= HTTP_SERVER_ERROR:
            _LOGGER.error("SecureControls: server error %s on login", resp.status)
            raise CannotConnect(f"Server error: {resp.status}")

        try:
            root = await resp.json()
        except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError) as err:
            txt = await resp.text()
            _LOGGER.error(
                "SecureControls: bad JSON from login (HTTP %s): %s", resp.status, txt[:400]
            )
            raise CannotConnect(f"Bad JSON from login (HTTP {resp.status})") from err

        # Some responses use an error envelope even with HTTP 200
        if isinstance(root, dict) and "C" in root and root.get("C") != HTTP_OK:
            c, ec, ea, msg = _decode_api_error(root)
            _LOGGER.error("SecureControls: login rejected (C=%s EC=%s) %s EA=%s", c, ec, msg, ea)
            if c == AUTH_ERROR_CODE and ec == AUTH_ERROR_SUBCODE:
                self._auth_rejected = True
                raise InvalidAuth("Login rejected: credentials/session invalid (C=203 EC=2)")
            raise ServerRejected(c, ec, ea)

        d = root.get("D") or {}
        jwt = d.get("JT")
        si = d.get("SI")
        gd = d.get("GD") or []

        if not jwt or not si:
            _LOGGER.warning(
                "SecureControls: login missing JT/SI. RI=%s, payload=%s",
                root.get("RI"),
                json.dumps(root)[:800],
            )
            raise InvalidAuth(f"Missing JT/SI in response (RI={root.get('RI')})")

        self._jwt = jwt
        self._session_id = si
        self._session_ts = d.get("JTT")
        self._user_id = d.get("UI")
        self._auth_rejected = False

        _LOGGER.debug(
            "SecureControls: login ok. SI=%s UI=%s JTT=%s, GD count=%s",
            self._session_id,
            self._user_id,
            self._session_ts,
            len(gd),
        )

        if not gd:
            _LOGGER.error("SecureControls: login ok but no devices (GD empty)")
            raise ApiError("Login ok but no devices (GD empty)")

        self.gateways = [g for g in gd if isinstance(g, dict)]
        gw = next(
            (
                g
                for g in self.gateways
                if self.preferred_gmi and str(g.get("GMI")) == self.preferred_gmi
            ),
            gd[0],
        )
        self.thermostat = Thermostat(
            gmi=str(gw["GMI"]),
            sn=str(gw["SN"]),
            hn=str(gw["HN"]),
            cs=gw.get("CS"),
            ur=gw.get("UR"),
            hi=gw.get("HI"),
            dt=gw.get("DT"),
            dn=gw.get("DN"),
        )
        _LOGGER.debug(
            "SecureControls: selected thermostat GMI=%s SN=%s HN=%s",
            self.thermostat.gmi,
            self.thermostat.sn,
            self.thermostat.hn,
        )

    # --------------- Persistent WebSocket lifecycle ---------------
    def _ws_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._jwt}",
            "Session-id": str(self._session_id),
            "Request-id": "1",
        }

    async def _open_websocket(self) -> aiohttp.ClientWebSocketResponse:
        if self._auth_rejected:
            raise InvalidAuth(
                "Beanbag authentication was rejected; reload the integration to sign in again"
            )
        if not self._jwt or not self._session_id:
            raise InvalidAuth("Call login() before sending a request")
        if not self.thermostat:
            raise RuntimeError("Call login() first")

        _LOGGER.debug("SecureControls: opening WebSocket to %s", WS_URL)
        try:
            websocket = await self._http.ws_connect(
                WS_URL,
                headers=self._ws_headers(),
                protocols=[WS_SUBPROTOCOL],
                heartbeat=None,
                autoping=True,
            )
        except aiohttp.WSServerHandshakeError as err:
            if err.status in (401, 403):
                self._auth_rejected = True
                raise InvalidAuth(
                    "Beanbag WebSocket authentication was rejected; reload the integration"
                ) from err
            raise CannotConnect(f"WebSocket handshake failed: HTTP {err.status}") from err
        except aiohttp.ClientError as err:
            raise CannotConnect(f"WebSocket connection failed: {err}") from err

        self._ws = websocket
        _LOGGER.debug("SecureControls: WebSocket connected (protocol=%s)", WS_SUBPROTOCOL)
        return websocket

    async def _close_websocket(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        if self._ws is websocket:
            self._ws = None
        if not websocket.closed:
            with contextlib.suppress(Exception):
                await websocket.close()

    async def disconnect(self) -> None:
        """Close the persistent socket during integration unload."""
        websocket = self._ws
        if websocket is not None:
            await self._close_websocket(websocket)

    # --------------- Transactional request/response ---------------
    async def _send_request(
        self,
        *,
        hi: int,
        si: int,
        args: list[Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        if not self.thermostat:
            raise RuntimeError("No thermostat selected")
        if self._auth_rejected:
            raise InvalidAuth(
                "Beanbag authentication was rejected; reload the integration to sign in again"
            )

        async with self._operation_lock:
            # Re-check after waiting for another operation, which may have latched
            # an authentication rejection.
            if self._auth_rejected:
                raise InvalidAuth(
                    "Beanbag authentication was rejected; reload the integration to sign in again"
                )

            if timeout is None:
                timeout = WS_RESPONSE_TIMEOUT_SECS
            corr = self._new_corr()
            env: Json = {
                "V": "1.0",
                "DTS": self._now_epoch(),
                "I": corr,
                "M": "Request",
                "P": [
                    {"GMI": int(self.thermostat.gmi), "HI": hi, "SI": si},
                ],
            }
            if args is not None:
                env["P"].append(args)

            websocket = self._ws
            if websocket is None or websocket.closed:
                websocket = await self._open_websocket()

            success = False
            try:
                text = _compact_dumps(env)
                if len(text.encode()) > MAX_REQUEST_BYTES:
                    _LOGGER.warning(
                        "SecureControls: request HI/SI=%s/%s is %s bytes; the device may ignore it",
                        hi,
                        si,
                        len(text.encode()),
                    )
                await websocket.send_json(env, dumps=_compact_dumps)
                _LOGGER.debug("SecureControls: sent request HI/SI=%s/%s corr=%s", hi, si, corr)

                async with asyncio.timeout(timeout):
                    while True:
                        message = await websocket.receive()
                        if message.type == aiohttp.WSMsgType.TEXT:
                            try:
                                payload = json.loads(str(message.data))
                            except json.JSONDecodeError:
                                _LOGGER.debug(
                                    "SecureControls: ignoring non-JSON WS frame: %s",
                                    str(message.data)[:120],
                                )
                                continue

                            if not isinstance(payload, dict) or payload.get("I") != corr:
                                continue
                            if "R" in payload:
                                success = True
                                return payload["R"]
                            if "E" in payload:
                                err_obj = payload.get("E") or {}
                                c, ec, ea, msgtxt = _decode_api_error(err_obj)
                                _LOGGER.warning(
                                    "SecureControls: WS error reply (C=%s EC=%s) %s EA=%s",
                                    c,
                                    ec,
                                    msgtxt,
                                    ea,
                                )
                                if c == AUTH_ERROR_CODE and ec == AUTH_ERROR_SUBCODE:
                                    self._auth_rejected = True
                                    raise InvalidAuth(
                                        "Beanbag session was rejected; reload the integration"
                                    )
                                raise ServerRejected(c, ec, ea)
                            raise CannotConnect("WebSocket response missing result payload")

                        if message.type in {
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.ERROR,
                        }:
                            raise CannotConnect("WebSocket closed before the response arrived")
            except TimeoutError as err:
                raise CannotConnect(
                    f"WebSocket response timed out after {timeout:g} seconds"
                ) from err
            except aiohttp.ClientError as err:
                raise CannotConnect(f"WebSocket request failed: {err}") from err
            finally:
                # Keep a successful transaction's socket open for reuse; any
                # other outcome (error reply, timeout, transport loss,
                # cancellation) leaves the socket unusable, so close it.
                if not success:
                    await self._close_websocket(websocket)

    # --------------- Reads (HI/SI pairs) ---------------
    async def zones_read(self) -> Any:
        # 49/11
        return await self._send_request(hi=49, si=11)

    async def time_tick(self) -> Any:
        # 2/103 with [epochSeconds]
        return await self._send_request(hi=2, si=103, args=[self._now_epoch()])

    async def device_metadata_read(self) -> Any:
        # 17/11
        return await self._send_request(hi=17, si=11)

    async def device_config_read(self) -> Any:
        # 14/11
        return await self._send_request(hi=14, si=11)

    async def state_read(self) -> Any:
        # 3/1 → returns blocks with items
        return await self._send_request(hi=3, si=1)

    async def program_read(self, index: int) -> Any:
        # 22/17 with [index] → weekly program; index = zone/channel number
        return await self._send_request(hi=22, si=17, args=[int(index)])

    async def program_write(self, index: int, entries: list[dict[str, int]]) -> Any:
        """21/17 with [{"I": index, "D": [7 days x 6 {"O","T"}]}] → replace a weekly program."""
        if len(entries) != 42:  # noqa: PLR2004
            raise ValueError("A weekly program has exactly 42 switch points (7 x 6)")
        return await self._send_request(
            hi=21,
            si=17,
            args=[{"I": int(index), "D": [dict(e) for e in entries]}],
            timeout=PROGRAM_WRITE_TIMEOUT_SECS,
        )

    # --------------- Generic writer helper ---------------
    async def _write_item(
        self,
        item_id: int,
        value: int,
        *,
        ot: int = 1,
        d: int = 0,
        slot: int = THERMO_SLOT,
        si: int = THERMO_SI,
    ) -> Any:
        """
        Write a single state item on block SI (default 15) / slot (default 1).
        ot: 1=immediate set, 2=timed override (minutes in D)
        Multi-zone programmers (H3747/C1727) use slot = zone/channel number.
        """
        return await self._send_request(
            hi=THERMO_HI_WRITE,
            si=si,
            args=[int(slot), {"I": int(item_id), "V": int(value), "OT": int(ot), "D": int(d)}],
        )

    # --------------- Writes (Thermostat SI:15, slot=1) ---------------
    async def set_target_temp(self, celsius: float, slot: int = THERMO_SLOT) -> Any:
        # I:1 target (deci °C), OT:1 immediate
        return await self._write_item(
            ITEM_TARGET, self.c_to_deci(celsius), ot=1, d=0, slot=slot
        )

    async def set_mode(self, on: bool) -> Any:
        """
        Backward-compatible name used by the climate entity.
        With the updated mapping, this toggles HVAC (I:3): 0=off, 1=heat.
        """
        hvac_val = 1 if on else 0
        return await self._write_item(ITEM_HVAC, hvac_val, ot=1, d=0)

    async def set_hvac(self, *, heat: bool) -> Any:
        """Alias that makes intent explicit."""
        return await self.set_mode(heat)

    async def set_preset(self, preset: str | int) -> Any:
        """
        Set preset (I:6): 1=away, 2=home.
        Accepts either 'away'/'home' (case-insensitive) or 1/2.
        """
        if isinstance(preset, str):
            p = preset.strip().lower()
            if p == "away":
                code = 1
            elif p == "home":
                code = 2
            else:
                raise ValueError(f"Unsupported preset '{preset}' (expected 'away' or 'home')")
        else:
            code = int(preset)
            if code not in (1, 2):
                raise ValueError(f"Unsupported preset code {code} (expected 1 or 2)")
        return await self._write_item(ITEM_PRESET, code, ot=1, d=0)

    async def set_timed_hold(self, celsius: float, minutes: int, slot: int = THERMO_SLOT) -> Any:
        # Timed override on target (I:1, OT:2) for D:<minutes>
        return await self._write_item(
            ITEM_TARGET, self.c_to_deci(celsius), ot=2, d=int(minutes), slot=slot
        )

    # --------------- Writes (hot water, SI:16) ---------------
    async def hot_water_boost(self, slot: int, minutes: int) -> Any:
        """Boost a hot water channel for <minutes> (same command as the app's Boost)."""
        if int(minutes) <= 0:
            raise ValueError("Boost duration must be a positive number of minutes")
        return await self._write_item(
            ITEM_HW_BOOST, 0, ot=2, d=int(minutes), slot=slot, si=HOT_WATER_SI
        )

    async def hot_water_cancel_boost(self, slot: int) -> Any:
        """Cancel an active hot water boost."""
        return await self._write_item(ITEM_HW_BOOST, 0, ot=2, d=0, slot=slot, si=HOT_WATER_SI)
