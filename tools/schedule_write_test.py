#!/usr/bin/env python3
"""
Secure Controls: find the schedule-write format a programmer accepts.

Needs secure_probe.py in the same folder (for login). Python 3.9+, pip install aiohttp.

Step 1 (always, changes nothing): reads a zone's weekly schedule, then sends it back
UNCHANGED in a few message layouts and records which one the programmer answers.
Because the schedule sent is identical to the stored one, nothing changes either way.

Step 2 (only with --change): makes the smallest real change -- the first switch point
on Sunday goes up 0.5 C -- using the layout from step 1, checks that the programmer
stored it, then puts it back and checks again.

    python3 schedule_write_test.py --zone 4
    python3 schedule_write_test.py --zone 4 --change

Saves schedule_write_test.json (identifiers redacted).
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import secrets
import sys
import time
from typing import Any

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from secure_probe import WS_SUBPROTOCOL, WS_URL, Redactor, login  # noqa: E402

REPLY_WAIT = 45  # seconds to wait for an answer to each attempt
CHECK_WAIT = 90  # seconds to keep re-reading after a real change

LAYOUTS = [
    ("list[{I,D}] (E7+ style)", lambda z, d: [{"I": z, "D": d}]),
    ("{I,D}", lambda z, d: {"I": z, "D": d}),
    ("[zone, D]", lambda z, d: [z, d]),
    ("[{I,D}] + index header", lambda z, d: [{"I": z, "D": d}, z]),
]


class Session:
    def __init__(self, ws, sid, gmi, log):
        self.ws, self.sid, self.gmi, self.log = ws, sid, gmi, log
        self.frames: list[dict[str, Any]] = []

    async def send(self, hi: int, si: int, args: Any, wait: float) -> dict[str, Any]:
        corr = f"{self.sid}-{secrets.token_hex(4)}"
        env = {"V": "1.0", "DTS": int(time.time()), "I": corr, "M": "Request",
               "P": [{"GMI": int(self.gmi), "HI": hi, "SI": si}]}
        if args is not None:
            env["P"].append(args)
        start = time.monotonic()
        await self.ws.send_json(env)
        while (left := wait - (time.monotonic() - start)) > 0:
            try:
                msg = await asyncio.wait_for(self.ws.receive(), left)
            except asyncio.TimeoutError:
                break
            if msg.type != aiohttp.WSMsgType.TEXT:
                return {"result": "socket closed", "code": self.ws.close_code,
                        "after_s": round(time.monotonic() - start, 1)}
            data = json.loads(msg.data)
            if data.get("I") == corr:
                took = round(time.monotonic() - start, 1)
                if "E" in data:
                    return {"result": "error", "error": data["E"], "after_s": took}
                return {"result": "reply", "R": data.get("R"), "after_s": took}
            self.frames.append({"t": round(time.time()), **data})
            if data.get("M") == "Notify":
                head, body = (data.get("P") or [{}, None])[:2]
                self.log(f"      notify SI={head.get('SI')} HI={head.get('HI')} {str(body)[:160]}")
        return {"result": "no reply", "after_s": wait}

    async def read_program(self, zone: int) -> list[dict[str, int]] | None:
        for _ in range(3):
            r = await self.send(22, 17, [zone], 45)
            if r["result"] == "reply" and isinstance(r["R"], list) and r["R"]:
                return r["R"][0].get("D")
        return None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zone", type=int, required=True, help="zone number (e.g. 4)")
    ap.add_argument("--change", action="store_true", help="also try a real +0.5 C change and undo it")
    ap.add_argument("--out", default="schedule_write_test.json")
    opts = ap.parse_args()

    email = os.environ.get("SECURE_EMAIL") or input("Secure Controls email: ")
    password = os.environ.get("SECURE_PASSWORD") or getpass.getpass("Password: ")
    red = Redactor()
    log = lambda m: print(red.scrub(m), flush=True)  # noqa: E731
    report: dict[str, Any] = {"zone": opts.zone, "layouts": [], "change": None}

    async with aiohttp.ClientSession() as http:
        d = await login(http, email, password)
        red.tag("UEI", email.strip())
        gw = (d.get("GD") or [None])[0]
        if not gw:
            sys.exit("No devices on this account")
        for k in ("GMI", "SN", "HN", "DN"):
            if gw.get(k) is not None:
                red.tag(k, gw[k])
        headers = {"Authorization": f"Bearer {d['JT']}", "Session-id": str(d["SI"]), "Request-id": "1"}
        async with http.ws_connect(WS_URL, headers=headers, protocols=[WS_SUBPROTOCOL]) as ws:
            s = Session(ws, d["SI"], gw["GMI"], log)
            original = await s.read_program(opts.zone)
            if not original:
                sys.exit(f"Could not read zone {opts.zone}'s schedule")
            report["original"] = original
            log(f"Read zone {opts.zone}'s schedule ({len(original)} points).\n")

            log("Step 1: sending the schedule back UNCHANGED in different layouts")
            working = None
            for name, build in LAYOUTS:
                log(f"  {name} ...")
                r = await s.send(21, 17, build(opts.zone, original), REPLY_WAIT)
                log(f"    -> {r['result']} after {r['after_s']}s"
                    + (f": {r.get('R', r.get('error'))}" if r['result'] in ('reply', 'error') else ""))
                report["layouts"].append({"layout": name, **r})
                if r["result"] == "socket closed":
                    log("    (connection closed; reconnecting is not supported here, stopping)")
                    break
                if r["result"] == "reply" and working is None:
                    working = (name, build)
                    break
            after = await s.read_program(opts.zone)
            report["unchanged_after_step1"] = after == original
            log(f"  schedule unchanged afterwards: {after == original}\n")

            if opts.change:
                name, build = working or LAYOUTS[0][:2]
                log(f"Step 2: real change using layout '{name}': Sunday first point +0.5 C")
                changed = [dict(e) for e in original]
                sun = 6 * 6
                changed[sun]["T"] += 5
                log(f"  Sunday {changed[sun]['O'] // 60:02d}:{changed[sun]['O'] % 60:02d}: "
                    f"{original[sun]['T'] / 10} -> {changed[sun]['T'] / 10} C")
                r = await s.send(21, 17, build(opts.zone, changed), REPLY_WAIT)
                log(f"    -> {r['result']} after {r['after_s']}s")
                stored_at = None
                start = time.monotonic()
                while time.monotonic() - start < CHECK_WAIT:
                    now = await s.read_program(opts.zone)
                    if now == changed:
                        stored_at = round(time.monotonic() - start, 1)
                        break
                    await asyncio.sleep(5)
                log(f"    stored on the programmer: {'yes' if stored_at is not None else 'NO'}")
                report["change"] = {"layout": name, "send": r, "stored": stored_at is not None,
                                    "stored_after_s": stored_at}
                if True:  # always restore, even if the change seemed not to land
                    log("  Putting it back ...")
                    r2 = await s.send(21, 17, build(opts.zone, original), REPLY_WAIT)
                    final = None
                    start = time.monotonic()
                    while time.monotonic() - start < CHECK_WAIT:
                        final = await s.read_program(opts.zone)
                        if final == original:
                            break
                        await asyncio.sleep(5)
                    restored = final == original
                    report["change"]["restore"] = {"send": r2, "restored": restored}
                    log(f"    restored: {'yes' if restored else 'NO -- please set Sunday back in the app'}")
            report["other_frames"] = s.frames

    with open(opts.out, "w") as f:
        json.dump(red.scrub(report), f, indent=2)
    log(f"\nSaved {opts.out}.")


if __name__ == "__main__":
    asyncio.run(main())
