"""Find scoreboard data sources for the setup wizard: ScoreBridge boxes on the network, and USB devices
(Sportzcast ScoreLinkII / ScoreBot, a ScoreBridge on USB, other USB-serial adapters).

Dependency-free (pyserial only for the USB list). Network search, in order:
  1. mDNS: a one-shot query for _scorebridge._tcp.local (the ScoreBridge firmware advertises it).
  2. The name scorebridge.local through the computer's own resolver (macOS, Windows 10+, Pi with avahi).
  3. Only when both find nothing: a quick scan of this computer's /24 for TCP port 10001, each hit
     confirmed by the ScoreBridge's http://<ip>/api/status.
"""
from __future__ import annotations

import concurrent.futures as _cf
import ipaddress
import json
import socket
import struct
import threading
import time
import urllib.request

MDNS_ADDR = ("224.0.0.251", 5353)
SERVICE = "_scorebridge._tcp.local"
DEFAULT_TCP_PORT = 10001
SCORELINK_IDS = {(0x04D8, 0x00DD)}
CH340_IDS = {(0x1A86, 0x7523), (0x1A86, 0x55D4), (0x1A86, 0x7522)}  # the ScoreBridge's USB chip (CH340C)


# --------------------------------------------------------------------------- mDNS (minimal)
def _qname(name: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode() for p in name.strip(".").split(".")) + b"\0"


def build_query(name: str = SERVICE, qtype: int = 12) -> bytes:
    """A one-question DNS query. The QU bit asks for a unicast reply, which is what we listen for."""
    return struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + _qname(name) + struct.pack(">HH", qtype, 0x8001)


def _read_name(buf: bytes, off: int) -> tuple[str, int]:
    labels, jumped, end, hops = [], False, off, 0
    while True:
        if off >= len(buf) or hops > 40:
            raise ValueError("bad name")
        n = buf[off]
        if n & 0xC0 == 0xC0:
            if not jumped:
                end = off + 2
            off = ((n & 0x3F) << 8) | buf[off + 1]
            jumped, hops = True, hops + 1
            continue
        if n == 0:
            if not jumped:
                end = off + 1
            return ".".join(labels), end
        labels.append(buf[off + 1:off + 1 + n].decode("utf-8", "replace"))
        off += 1 + n


def parse_response(buf: bytes) -> dict:
    """Records from one mDNS packet: {"ptr": [instance], "srv": {instance: (target, port)}, "a": {host: ip}}."""
    out = {"ptr": [], "srv": {}, "a": {}}
    if len(buf) < 12:
        return out
    _id, flags, qd, an, ns, ar = struct.unpack(">HHHHHH", buf[:12])
    off = 12
    try:
        for _ in range(qd):
            _, off = _read_name(buf, off)
            off += 4
        for _ in range(an + ns + ar):
            name, off = _read_name(buf, off)
            rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", buf[off:off + 10])
            off += 10
            rd = off
            off += rdlen
            if rtype == 12:  # PTR
                out["ptr"].append(_read_name(buf, rd)[0])
            elif rtype == 33:  # SRV
                _pri, _w, port = struct.unpack(">HHH", buf[rd:rd + 6])
                out["srv"][name.lower()] = (_read_name(buf, rd + 6)[0], port)
            elif rtype == 1 and rdlen == 4:  # A
                out["a"][name.lower()] = socket.inet_ntoa(buf[rd:rd + 4])
    except (ValueError, struct.error, IndexError):
        pass
    return out


def mdns_browse(timeout: float = 1.5) -> list[dict]:
    """[{name, host, ip, port}] for every ScoreBridge that answers the mDNS query."""
    found: dict[str, dict] = {}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        s.bind(("", 0))
        s.settimeout(0.2)
        s.sendto(build_query(), MDNS_ADDR)
    except OSError:
        return []
    end = time.time() + timeout
    try:
        while time.time() < end:
            try:
                buf, (src, _p) = s.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError:
                break
            r = parse_response(buf)
            for inst in r["ptr"] + list(r["srv"]):
                if SERVICE not in inst.lower():
                    continue
                target, port = r["srv"].get(inst.lower(), ("", DEFAULT_TCP_PORT))
                ip = r["a"].get(target.lower(), src)
                key = ip
                found[key] = {"name": inst.split(".")[0], "host": target.rstrip(".") or "", "ip": ip, "port": port}
    finally:
        s.close()
    return list(found.values())


# --------------------------------------------------------------------------- helpers
def _resolve(name: str, timeout: float = 1.5) -> str | None:
    res: list = []

    def go():
        try:
            res.append(socket.gethostbyname(name))
        except OSError:
            pass

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout)
    return res[0] if res else None


def bridge_status(ip: str, timeout: float = 1.2) -> dict | None:
    """The ScoreBridge's /api/status, or None if this isn't a ScoreBridge."""
    try:
        with urllib.request.urlopen(f"http://{ip}/api/status", timeout=timeout) as r:
            j = json.loads(r.read(8192).decode("utf-8", "replace"))
        return j if isinstance(j, dict) and "tcp_port" in j else None
    except Exception:
        return None


def local_ipv4() -> str | None:
    """This computer's LAN address (no packet is sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        finally:
            s.close()
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


def _port_open(ip: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan_subnet(port: int = DEFAULT_TCP_PORT, timeout: float = 0.35, workers: int = 64) -> list[str]:
    """IPs in this computer's /24 with `port` open."""
    me = local_ipv4()
    if not me:
        return []
    net = ipaddress.ip_network(me + "/24", strict=False)
    hosts = [str(h) for h in net.hosts() if str(h) != me]
    with _cf.ThreadPoolExecutor(workers) as ex:
        return [ip for ip, ok in zip(hosts, ex.map(lambda h: _port_open(h, port, timeout), hosts)) if ok]


def _bridge_entry(ip: str, port: int, host: str, how: str, status: dict | None) -> dict:
    st = status or {}
    port = int(st.get("tcp_port") or port or DEFAULT_TCP_PORT)
    receiving = bool(st.get("recent_hex")) and int(st.get("last_byte_ms") or 0) < 10_000
    detail = f"Network · {ip} port {port}"
    if st:
        detail += " · " + ("receiving data" if receiving else "no data from the console yet")
    return {"kind": "scorebridge", "title": "ScoreBridge" + (f" · {host}" if host else ""),
            "detail": detail, "found_by": how, "receiving": receiving, "version": st.get("version", ""),
            "settings": {"source": "tcp", "tcp.host": host or ip, "tcp.port": port}, "ip": ip}


def find_bridges(scan: bool = True) -> list[dict]:
    out: dict[str, dict] = {}
    for b in mdns_browse():
        host = b["host"] if b["host"].endswith(".local") else ""
        out[b["ip"]] = _bridge_entry(b["ip"], b["port"], host, "mdns", bridge_status(b["ip"]))
    ip = _resolve("scorebridge.local")
    if ip and ip not in out:
        st = bridge_status(ip)
        if st:
            out[ip] = _bridge_entry(ip, DEFAULT_TCP_PORT, "scorebridge.local", "name", st)
    if scan and not out:
        for ip in scan_subnet():
            st = bridge_status(ip)
            if st:
                # mDNS didn't work here, so use the IP address (give the box a fixed IP on its settings page)
                out[ip] = _bridge_entry(ip, DEFAULT_TCP_PORT, "", "scan", st)
    return list(out.values())


def find_usb(ports=None) -> list[dict]:
    """USB serial devices, ScoreBot first. `ports` = pyserial ListPortInfo list (for tests)."""
    if ports is None:
        try:
            from serial.tools import list_ports

            ports = list(list_ports.comports())
        except Exception:
            ports = []
    out = []
    for p in ports:
        dev = p.device
        if "bluetooth" in dev.lower() or (p.vid is None and not str(p.description or "").strip("n/a ")):
            continue
        ids = (p.vid, p.pid)
        txt = f"{p.description} {getattr(p, 'manufacturer', '')} {getattr(p, 'product', '')}".lower()
        if ids in SCORELINK_IDS or "scorelink" in txt or "mcp2221" in txt:
            out.append({"kind": "scorelink", "title": "Sportzcast ScoreBot (ScoreLinkII)", "detail": f"USB · {dev}",
                        "settings": {"source": "scorelink", "port": "auto"}, "rank": 1})
        elif ids in CH340_IDS or "ch340" in txt or "ch9102" in txt:
            out.append({"kind": "scorebridge_usb", "title": "ScoreBridge on USB",
                        "detail": f"{dev} · 115200 (any CH340 USB-serial cable shows up the same way)",
                        "settings": {"source": "serial", "port": dev, "baud": 115200}, "rank": 2})
        elif p.vid is not None:
            out.append({"kind": "serial", "title": f"USB-serial: {p.description or dev}", "detail": f"{dev}",
                        "settings": {"source": "serial", "port": dev}, "rank": 3})
    out.sort(key=lambda d: d["rank"])
    for d in out:
        d.pop("rank", None)
    return out


def discover(scan: bool = True) -> list[dict]:
    """Everything found, best first: a ScoreBridge receiving data, other ScoreBridges, then USB devices."""
    with _cf.ThreadPoolExecutor(2) as ex:
        fb = ex.submit(find_bridges, scan)
        fu = ex.submit(find_usb)
        bridges, usb = fb.result(), fu.result()
    bridges.sort(key=lambda b: not b["receiving"])
    return bridges + usb
