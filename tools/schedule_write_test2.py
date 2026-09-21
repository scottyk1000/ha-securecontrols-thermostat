#!/usr/bin/env python3
"""
Secure Controls: find which detail makes a schedule write work (second round).

A capture of the official app showed its schedule write is the same command we send
(HI:21 SI:17, [{"D":[42 points], "I":zone}]) but with three differences:
  a) it sends a clock sync (HI:2 SI:103 [epoch]) right after connecting,
  b) its message IDs are "<session>-<digits>" rather than hex,
  c) the schedule field comes before the zone number ({"D":..., "I":...}).

This tool sends the zone's schedule back UNCHANGED (so nothing changes either way) with
each combination, to see which ones the programmer answers. With --change it then makes
one small real change (Sunday's first point +0.5 C) the way that works, checks it was
stored, and puts it back.

    python3 schedule_write_test2.py --zone 4 --change

Needs secure_probe.py in the same folder. Saves schedule_write_test2.json.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import random
import secrets
import sys
import time
from typing import Any

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from secure_probe import WS_SUBPROTOCOL, WS_URL, Redactor, login  # noqa: E402

REPLY_WAIT = 30
CHECK_WAIT = 60


class Session:
    def __init__(self, ws, sid, gmi, log):
        self.ws, self.sid, self.gmi, self.log = ws, sid, int(gmi), log

    def corr(self, numeric: bool) -> str:
        return f"{self.sid}-{random.randint(10**8, 4 * 10**9) if numeric else secrets.token_hex(4)}"

    async def send(self, hi, si, args, *, numeric=True, compact=True, wait=REPLY_WAIT) -> dict[str, Any]:
        corr = self.corr(numeric)
        header = {"GMI": self.gmi, "HI": hi, "SI": si}
        env: dict[str, Any] = {"V": "1.0", "DTS": int(time.time()), "I": corr, "M": "Request",
                               "P": [header] + ([args] if args is not None else [])}
        start = time.monotonic()
        text = json.dumps(env, separators=(",", ":")) if compact else json.dumps(env)
        await self.ws.send_str(text)
        while (left := wait - (time.monotonic() - start)) > 0:
            try:
                msg = await asyncio.wait_for(self.ws.receive(), left)
            except asyncio.TimeoutError:
                break
            if msg.type != aiohttp.WSMsgType.TEXT:
                return {"result": "socket closed", "after_s": round(time.monotonic() - start, 1)}
            data = json.loads(msg.data)
            if data.get("I") == corr:
                took = round(time.monotonic() - start, 1)
                if "E" in data:
                    return {"result": "error", "error": data["E"], "after_s": took}
                return {"result": "reply", "R": data.get("R"), "after_s": took}
        return {"result": "no reply", "after_s": wait}

    async def read_program(self, zone: int):
        for _ in range(3):
            r = await self.send(22, 17, [zone], wait=45)
            if r["result"] == "reply" and isinstance(r["R"], list) and r["R"]:
                return r["R"][0].get("D")
        return None

    async def write_program(self, zone: int, points, *, numeric: bool, d_first: bool, compact=True):
        body = {"D": points, "I": zone} if d_first else {"I": zone, "D": points}
        return await self.send(21, 17, [body], numeric=numeric, compact=compact)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zone", type=int, required=True)
    ap.add_argument("--change", action="store_true", help="also try a real +0.5 C change and undo it")
    ap.add_argument("--out", default="schedule_write_test2.json")
    opts = ap.parse_args()

    email = os.environ.get("SECURE_EMAIL") or input("Secure Controls email: ")
    password = os.environ.get("SECURE_PASSWORD") or getpass.getpass("Password: ")
    red = Redactor()
    log = lambda m: print(red.scrub(m), flush=True)  # noqa: E731
    report: dict[str, Any] = {"zone": opts.zone, "tests": []}

    async with aiohttp.ClientSession() as http:
        d = await login(http, email, password)
        red.tag("UEI", email.strip())
        gw = (d.get("GD") or [None])[0]
        for k in ("GMI", "SN", "HN", "DN"):
            if gw.get(k) is not None:
                red.tag(k, gw[k])
        headers = {"Authorization": f"Bearer {d['JT']}", "Session-id": str(d["SI"]), "Request-id": "1"}
        async with http.ws_connect(WS_URL, headers=headers, protocols=[WS_SUBPROTOCOL]) as ws:
            s = Session(ws, d["SI"], gw["GMI"], log)
            await s.send(49, 11, None)  # zones.read, as the app does
            original = await s.read_program(opts.zone)
            if not original:
                sys.exit(f"Could not read zone {opts.zone}'s schedule")
            report["original"] = original
            log(f"Read zone {opts.zone}'s schedule ({len(original)} points).")
            log("Sending it back UNCHANGED in different ways (nothing will change):\n")

            async def trial(name, numeric, d_first, compact=True):
                r = await s.write_program(opts.zone, original, numeric=numeric, d_first=d_first,
                                          compact=compact)
                ok = r["result"] == "reply"
                log(f"  {'ANSWERED ' if ok else 'no answer'}  {name}  ({r['result']} after {r['after_s']}s)")
                report["tests"].append({"test": name, **r})
                return ok

            # Round 2 finding: our writes were 1083 bytes with spaces, the app's 898 compact.
            results = {}
            results["compact only (old IDs/order, no sync)"] = await trial(
                "compact only (old IDs/order, no sync)", False, False)
            if not results["compact only (old IDs/order, no sync)"]:
                tick = await s.send(2, 103, [int(time.time())])
                log(f"\n  (clock sync sent: {tick['result']})\n")
                report["clock_sync"] = tick
                results["compact + everything app-style"] = await trial(
                    "compact + everything app-style", True, True)

            unchanged = (await s.read_program(opts.zone)) == original
            report["unchanged_after_tests"] = unchanged
            log(f"\n  schedule still unchanged: {unchanged}")

            if opts.change:
                log("\nReal change (compact, app-style): Sunday first point +0.5 C")
                changed = [dict(e) for e in original]
                changed[36]["T"] += 5
                r = await s.write_program(opts.zone, changed, numeric=True, d_first=True)
                log(f"  -> {r['result']} after {r['after_s']}s")
                stored = False
                start = time.monotonic()
                while time.monotonic() - start < CHECK_WAIT:
                    if await s.read_program(opts.zone) == changed:
                        stored = True
                        break
                    await asyncio.sleep(3)
                log(f"  stored on the programmer: {'YES' if stored else 'no'}")
                r2 = await s.write_program(opts.zone, original, numeric=True, d_first=True)
                restored = False
                start = time.monotonic()
                while time.monotonic() - start < CHECK_WAIT:
                    if await s.read_program(opts.zone) == original:
                        restored = True
                        break
                    await asyncio.sleep(3)
                log(f"  put back: {'yes' if restored else 'NO -- please set Sunday back in the app'}")
                report["change"] = {"send": r, "stored": stored, "restore": r2, "restored": restored}

    with open(opts.out, "w") as f:
        json.dump(red.scrub(report), f, indent=2)
    log(f"\nSaved {opts.out}.")


if __name__ == "__main__":
    asyncio.run(main())
