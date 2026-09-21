#!/usr/bin/env python3
"""
Secure Controls (Beanbag cloud) probe  -- READ-ONLY

Captures what a Secure Controls device (H3747, C1727, thermostats, ...) reports
to the cloud, so support for new models and features can be added.

It logs in with your Secure Controls app account, runs the same read-only
requests the app makes on refresh and, with --listen, records every value that
changes while you use the app. The result is saved to secure_probe.json with
serial numbers, gateway IDs, names, location and your email replaced by
placeholders, so it can be attached to a GitHub issue.

It never sends a command to your device.

Usage (Python 3.9+):
    pip install aiohttp
    python3 secure_probe.py --listen 240
    python3 secure_probe.py --listen 300 --schedule 4   # also watch zone 4's schedule

While it says "Watching", make one change at a time in the app, ~20 s apart,
and note the order (e.g. zone 1 target +1, hot water boost on, boost off).
Credentials are prompted for (or read from SECURE_EMAIL / SECURE_PASSWORD) and
are only sent to app.beanbag.online, the server the official app uses.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import secrets
import sys
import time
from typing import Any

import aiohttp

BASE = "https://app.beanbag.online"
WS_URL = f"{BASE}/api/TransactionRestAPI/ConnectWebSocket".replace("https://", "wss://")
WS_SUBPROTOCOL = "BB-BO-01"
TIMEOUT = 15

# (label, HI, SI, args) -- all read-only, taken from the app's refresh burst
READS: list[tuple[str, int, int, list[Any] | None]] = [
    ("zones.read", 49, 11, None),
    ("schedules.summary", 5, 1, None),
    ("device.metadata.read", 17, 11, None),
    ("device.config.read", 14, 11, None),
    ("state.read", 3, 1, None),
]
# Weekly programs: index = zone/channel (E7+ uses 1-2, H3747 1-4)
PROGRAM_INDEXES = range(1, 13)

REDACT_KEYS = {"GMI", "SN", "HN", "DN", "N", "UEI", "EI", "E", "MAC", "IP", "MC", "L", "LO"}


class Redactor:
    """Replace identifying values with stable placeholders (same value -> same tag)."""

    def __init__(self) -> None:
        self.map: dict[str, str] = {}

    def tag(self, key: str, value: Any) -> str:
        s = str(value)
        if s not in self.map:
            self.map[s] = f"<{key}_{len(self.map) + 1}>"
        return self.map[s]

    def scrub(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            return {
                k: (self.tag(k, v) if k in REDACT_KEYS and isinstance(v, (str, int)) and not isinstance(v, bool)
                    and not (k == "E" and isinstance(v, dict)) else self.scrub(v))
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [self.scrub(v) for v in obj]
        if isinstance(obj, str):
            for real, tag in self.map.items():
                if real and len(real) > 3 and real in obj:
                    obj = obj.replace(real, tag)
        return obj


async def login(http: aiohttp.ClientSession, email: str, password: str) -> dict[str, Any]:
    payload = {
        "ULC": {
            "OI": 1550005,
            "NT": "SetLogin",
            "UEI": email.strip(),
            "P": hashlib.md5(password.encode()).hexdigest(),
        }
    }
    headers = {"Content-Type": "application/json;charset=UTF-8", "Request-id": "1"}
    async with http.post(f"{BASE}/api/UserRestAPI/LoginRequest", json=payload, headers=headers) as r:
        root = await r.json(content_type=None)
    d = (root or {}).get("D") or {}
    if not d.get("JT") or not d.get("SI"):
        sys.exit(f"Login failed: {json.dumps(root)[:300]}")
    return d


class Conn:
    """WebSocket that records why it closed and reconnects (with the same session) on demand."""

    def __init__(self, http, headers, log):
        self.http, self.headers, self.log = http, headers, log
        self.ws = None
        self.closes: list[dict[str, Any]] = []

    async def ensure(self):
        if self.ws is None or self.ws.closed:
            if self.ws is not None:
                self.closes.append({"close_code": self.ws.close_code, "exception": repr(self.ws.exception())})
                self.log(f"  (socket was closed, code={self.ws.close_code}; reconnecting)")
            self.ws = await self.http.ws_connect(WS_URL, headers=self.headers, protocols=[WS_SUBPROTOCOL])
        return self.ws

    async def request(self, sid, gmi, hi, si, args, notifies, retry=True):
        ws = await self.ensure()
        corr = f"{sid}-{secrets.token_hex(4)}"
        env = {"V": "1.0", "DTS": int(time.time()), "I": corr, "M": "Request",
               "P": [{"GMI": int(gmi), "HI": hi, "SI": si}]}
        if args is not None:
            env["P"].append(args)
        try:
            await ws.send_json(env)
            deadline = time.monotonic() + TIMEOUT
            while True:
                msg = await asyncio.wait_for(ws.receive(), max(0.1, deadline - time.monotonic()))
                if msg.type != aiohttp.WSMsgType.TEXT:
                    raise ConnectionError(f"socket closed (type={msg.type}, code={ws.close_code}, extra={msg.extra!r})")
                data = json.loads(msg.data)
                if data.get("M") == "Notify":
                    notifies.append({"t": round(time.time()), **data})
                    continue
                if data.get("I") != corr:
                    notifies.append({"t": round(time.time()), "_other": True, **data})
                    continue
                if "E" in data:
                    return {"error": data["E"]}
                return data.get("R")
        except (ConnectionError, aiohttp.ClientError) as err:
            self.closes.append({"during": f"{hi}/{si} {args}", "error": repr(err)})
            if not retry:
                raise
            await self.ensure()
            return await self.request(sid, gmi, hi, si, args, notifies, retry=False)


def flatten_state(r: Any) -> dict[str, Any]:
    """state.read payload -> {"SI15/slot1/item2": value, ...} for easy diffing."""
    flat: dict[str, Any] = {}
    if isinstance(r, dict):
        for block in r.get("V") or []:
            if isinstance(block, dict):
                for it in block.get("V") or []:
                    if isinstance(it, dict):
                        key = f"SI{block.get('SI')}/slot{block.get('I')}/item{it.get('I')}"
                        flat[key] = it.get("V") if not it.get("OT") else [it.get("V"), f"OT{it.get('OT')}", f"D{it.get('D')}"]
    return flat


DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _program_entries(r: Any) -> list | None:
    if isinstance(r, list) and r and isinstance(r[0], dict):
        return r[0].get("D")
    return None


def _fmt_point(e: Any) -> str:
    if not isinstance(e, dict):
        return repr(e)
    o, t = e.get("O"), e.get("T")
    if o == 65535:
        return "unused"
    return f"{o // 60:02d}:{o % 60:02d}={t}" if isinstance(o, int) else repr(e)


async def watch(conn, sid, gmi, variant, seconds, notifies, changes, log, program=None) -> None:
    log(f"\nWatching for {seconds}s -- make your changes in the app now, one at a time.")
    log("Changes appear below as they are seen (state is re-read every 10s).")
    if program:
        log(f"Zone {program}'s weekly schedule is re-read every 10s too.")
    start = time.monotonic()
    prev: dict[str, Any] | None = None
    prev_prog: list | None = None
    while (elapsed := time.monotonic() - start) < seconds:
        n_before = len(notifies)
        try:
            r = await conn.request(sid, gmi, *variant, notifies)
            cur = flatten_state(r)
        except Exception as err:
            log(f"  +{elapsed:>4.0f}s read failed: {err!r}")
            cur = None
        for nt in notifies[n_before:]:
            head, body = (nt.get("P") or [{}, None])[:2]
            log(f"  +{elapsed:>4.0f}s NOTIFY SI={head.get('SI')} {body}")
        if cur is not None and prev is not None:
            for k in sorted(set(cur) | set(prev)):
                if cur.get(k) != prev.get(k):
                    changes.append({"t": round(elapsed), "key": k, "from": prev.get(k), "to": cur.get(k)})
                    log(f"  +{elapsed:>4.0f}s {k}: {prev.get(k)} -> {cur.get(k)}")
        if cur is not None:
            prev = cur
        if program:
            try:
                prog = _program_entries(await conn.request(sid, gmi, 22, 17, [program], notifies))
            except Exception as err:
                log(f"  +{elapsed:>4.0f}s schedule read failed: {err!r}")
                prog = None
            if prog is not None and prev_prog is not None and prog != prev_prog:
                changes.append({"t": round(elapsed), "key": f"program[{program}]", "from": prev_prog, "to": prog})
                for i in range(max(len(prog), len(prev_prog))):
                    a = prev_prog[i] if i < len(prev_prog) else None
                    b = prog[i] if i < len(prog) else None
                    if a != b:
                        log(f"  +{elapsed:>4.0f}s schedule {DAY_NAMES[i // 6 % 7]} point {i % 6 + 1}: "
                            f"{_fmt_point(a)} -> {_fmt_point(b)}")
            if prog is not None:
                prev_prog = prog
        # idle-listen until the next poll so pushed Notify frames are captured too
        wait_until = time.monotonic() + 10
        while (left := wait_until - time.monotonic()) > 0:
            try:
                ws = await conn.ensure()
                msg = await asyncio.wait_for(ws.receive(), left)
            except (asyncio.TimeoutError, aiohttp.ClientError):
                break
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data.get("M") == "Notify":
                    notifies.append({"t": round(time.time()), **data})
                    head, body = (data.get("P") or [{}, None])[:2]
                    log(f"  +{time.monotonic() - start:>4.0f}s NOTIFY SI={head.get('SI')} HI={head.get('HI')} {str(body)[:300]}")
                else:
                    notifies.append({"t": round(time.time()), "_other": True, **data})
                    log(f"  +{time.monotonic() - start:>4.0f}s OTHER {str(data)[:300]}")
            else:
                conn.closes.append({"during": "idle", "close_code": ws.close_code})
                break


# Read-only state reads to try; 3/1 works on E7+ and H3747, variants for other models
STATE_VARIANTS: list[tuple[int, int, Any]] = [(3, 1, None), (3, 1, [1]), (3, 15, None), (3, 15, [1])]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", type=int, default=0, help="seconds to watch for changes you make in the app")
    ap.add_argument("--out", default="secure_probe.json")
    ap.add_argument("--schedule", type=int, default=0, metavar="ZONE",
                    help="while watching, also re-read this zone's weekly schedule every 10s")
    opts = ap.parse_args()

    email = os.environ.get("SECURE_EMAIL") or input("Secure Controls email: ")
    password = os.environ.get("SECURE_PASSWORD") or getpass.getpass("Password: ")

    red = Redactor()
    out: dict[str, Any] = {"probe_version": 2, "gateways": []}
    log = lambda m: print(red.scrub(m) if isinstance(m, str) else m, flush=True)  # noqa: E731

    async with aiohttp.ClientSession() as http:
        d = await login(http, email, password)
        red.tag("UEI", email.strip())
        gateways = d.get("GD") or []
        log(f"Logged in. {len(gateways)} gateway(s) on this account.")
        for gw in gateways:  # register identifiers before anything is scrubbed
            for k in ("GMI", "SN", "HN", "DN"):
                if gw.get(k) is not None:
                    red.tag(k, gw[k])

        headers = {"Authorization": f"Bearer {d['JT']}", "Session-id": str(d["SI"]), "Request-id": "1"}
        conn = Conn(http, headers, log)
        notifies: list = []
        sid = d["SI"]
        for gw in gateways:
            gmi = gw["GMI"]
            entry: dict[str, Any] = {"login_GD_entry": gw, "reads": {}, "changes": []}
            log(f"\nGateway {red.tag('GMI', gmi)}  DT={gw.get('DT')}")
            reads = [(l, h, s_, a) for l, h, s_, a in READS if l != "state.read"]
            reads += [(f"state.read {h}/{s_} {a}", h, s_, a) for h, s_, a in STATE_VARIANTS]
            reads += [(f"program.read[{i}]", 22, 17, [i]) for i in PROGRAM_INDEXES]
            working_state = None
            for label, hi, si, args in reads:
                try:
                    res = await conn.request(sid, gmi, hi, si, args, notifies)
                    entry["reads"][label] = res
                    ok = not (isinstance(res, dict) and "error" in res)
                    log(f"  {'ok  ' if ok else 'ERR '} {label}")
                    if ok and label.startswith("state.read") and working_state is None and flatten_state(res):
                        working_state = (hi, si, args)
                except Exception as err:  # keep going; failures are informative too
                    entry["reads"][label] = {"exception": repr(err)}
                    log(f"  FAIL {label}: {err!r}")
            if opts.listen > 0:
                if working_state is None:
                    log("\nNo state read worked; watching for pushed updates only.")
                    working_state = (17, 11, None)  # harmless read to keep the socket busy
                await watch(conn, sid, gmi, working_state, opts.listen, notifies, entry["changes"], log,
                            program=opts.schedule or None)
            out["gateways"].append(entry)
        out["notifies"] = notifies
        out["socket_events"] = conn.closes
        if conn.ws is not None:
            await conn.ws.close()

    with open(opts.out, "w") as f:
        json.dump(red.scrub(out), f, indent=2)
    log(f"\nSaved {opts.out} (identifiers redacted). Please look through it before sharing.")


if __name__ == "__main__":
    asyncio.run(main())
