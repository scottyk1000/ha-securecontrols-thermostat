"""
mitmproxy add-on: record how the Secure Controls app talks to the cloud or the programmer.

    mitmweb --mode wireguard -s mitm_beanbag_ws.py      # captures ALL phone traffic
    mitmdump -s mitm_beanbag_ws.py                        # regular proxy mode

Recorded to beanbag_ws.jsonl (in the folder you run it from):
  * WebSocket messages to *.beanbag.online
  * any raw TCP/UDP traffic to beanbag servers, to devices on the home network
    (192.168.x / 10.x / 172.16-31.x), or to unusual ports -- i.e. anything that is not
    ordinary web traffic, which is how an app would talk directly to the programmer.
Login tokens travel in HTTP headers, which are NOT written to the file.
"""
import ipaddress
import json
import time

from mitmproxy import http, tcp, udp

OUT = "beanbag_ws.jsonl"
NOISY_PORTS = {53, 123, 443, 5223, 5353}  # DNS, NTP, HTTPS, Apple push, mDNS


def _write(rec: dict) -> None:
    rec["time"] = time.strftime("%H:%M:%S")
    with open(OUT, "a") as f:
        f.write(json.dumps(rec) + "\n")


def _show(content: bytes) -> str:
    try:
        text = content.decode("utf-8")
        if text.isprintable() or "{" in text:
            return text
    except UnicodeDecodeError:
        pass
    return "hex:" + content.hex()


def _interesting(host: str, port: int) -> bool:
    if "beanbag" in (host or ""):
        return True
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private and not ip.is_loopback:
            return True
    except ValueError:
        pass
    return port not in NOISY_PORTS


# ---------- WebSocket (cloud live connection) ----------

def websocket_start(flow: http.HTTPFlow) -> None:
    if "beanbag" in flow.request.pretty_host:
        print(f"\n*** LIVE CONNECTION OPENED: {flow.request.path}\n", flush=True)
        _write({"event": "ws open", "path": flow.request.path})


def websocket_message(flow: http.HTTPFlow) -> None:
    if "beanbag" not in flow.request.pretty_host or flow.websocket is None:
        return
    msg = flow.websocket.messages[-1]
    text = msg.text if msg.is_text else msg.content.hex()
    direction = "APP -> CLOUD" if msg.from_client else "cloud -> app"
    print(f"{time.strftime('%H:%M:%S')} WS {direction}: {text[:300]}", flush=True)
    _write({"kind": "ws", "dir": direction, "text": text})


# ---------- raw TCP / UDP (direct or non-web connections) ----------

def _peer(flow) -> tuple[str, int]:
    addr = flow.server_conn.address or ("?", 0)
    host = flow.server_conn.sni or addr[0]
    return str(host), int(addr[1])


def tcp_start(flow: tcp.TCPFlow) -> None:
    host, port = _peer(flow)
    if _interesting(host, port):
        print(f"\n*** TCP CONNECTION to {host}:{port}\n", flush=True)
        _write({"event": "tcp open", "to": f"{host}:{port}"})


def tcp_message(flow: tcp.TCPFlow) -> None:
    host, port = _peer(flow)
    if not _interesting(host, port):
        return
    msg = flow.messages[-1]
    direction = "APP ->" if msg.from_client else "<- DEVICE/SERVER"
    shown = _show(msg.content)
    print(f"{time.strftime('%H:%M:%S')} TCP {host}:{port} {direction} {shown[:300]}", flush=True)
    _write({"kind": "tcp", "to": f"{host}:{port}", "dir": direction, "data": shown})


def udp_message(flow: udp.UDPFlow) -> None:
    host, port = _peer(flow)
    if not _interesting(host, port):
        return
    msg = flow.messages[-1]
    direction = "APP ->" if msg.from_client else "<- DEVICE/SERVER"
    shown = _show(msg.content)
    print(f"{time.strftime('%H:%M:%S')} UDP {host}:{port} {direction} {shown[:300]}", flush=True)
    _write({"kind": "udp", "to": f"{host}:{port}", "dir": direction, "data": shown})


# ---------- also note non-standard HTTP(S) destinations ----------

def request(flow: http.HTTPFlow) -> None:
    host, port = flow.request.pretty_host, flow.request.port
    if _interesting(host, port) and "beanbag" not in host:
        print(f"{time.strftime('%H:%M:%S')} HTTP to {host}:{port} {flow.request.method} "
              f"{flow.request.path[:120]}", flush=True)
        _write({"kind": "http", "to": f"{host}:{port}", "method": flow.request.method,
                "path": flow.request.path, "body": _show(flow.request.content or b"")[:4000]})
