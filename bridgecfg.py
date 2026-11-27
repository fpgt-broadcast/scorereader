"""ScoreBridge network settings (DHCP or a static Ethernet address), over USB or over the network.

Network: GET/POST http://<bridge>/api/network (firmware 1.1+).
USB:     one text line on the CH340 port at 115200, "@SB GET" or "@SB NET host=.. ip=.. mask=.. gw=.. dns=..";
         the box answers with one JSON object between REPLY_START and REPLY_END, in the middle of the console
         data it is streaming. ReplySplitter takes the reply back out so the decoders never see it.
Dependency-free apart from pyserial (only for USB).
"""
from __future__ import annotations

import ipaddress
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

REPLY_START = b"\r\n@SB>"
REPLY_END = b"<@SB\r\n"
USB_BAUD = 115200
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,31}$")


class BridgeError(Exception):
    pass


# --------------------------------------------------------------------------- settings
def clean_network(d: dict) -> dict:
    """Check DHCP / static settings from the dashboard. Returns {host, ip, mask, gw, dns}; ip "" = DHCP."""
    host = str(d.get("hostname") or d.get("host") or "scorebridge").strip()
    if not _HOST_RE.match(host):
        raise ValueError("Name: letters, digits and - only (up to 32)")
    out = {"host": host, "ip": "", "mask": "", "gw": "", "dns": ""}
    if d.get("dhcp", not d.get("ip")):
        return out
    labels = {"ip": "IP address", "mask": "Subnet mask", "gw": "Gateway", "dns": "DNS"}
    for k, src in (("ip", "ip"), ("mask", "mask"), ("gw", "gateway"), ("dns", "dns")):
        v = str(d.get(src, d.get(k, "")) or "").strip()
        if k == "mask" and not v:
            v = "255.255.255.0"
        if not v:
            if k == "ip":
                raise ValueError("Type the IP address for the ScoreBridge, or pick DHCP")
            continue
        try:
            ipaddress.IPv4Address(v)
        except ValueError:
            raise ValueError(f"{labels[k]} '{v}' is not an IPv4 address") from None
        out[k] = v
    try:
        net = ipaddress.IPv4Network(f"{out['ip']}/{out['mask']}", strict=False)
    except ValueError:
        raise ValueError(f"Subnet mask '{out['mask']}' doesn't look right (usually 255.255.255.0)") from None
    if net.prefixlen < 8 or net.prefixlen > 30 or ipaddress.IPv4Address(out["mask"]) != net.netmask:
        raise ValueError(f"Subnet mask '{out['mask']}' doesn't look right (usually 255.255.255.0)")
    ip = ipaddress.IPv4Address(out["ip"])
    if ip in (net.network_address, net.broadcast_address):
        raise ValueError(f"{out['ip']} can't be used with mask {out['mask']}")
    if out["gw"] and ipaddress.IPv4Address(out["gw"]) not in net:
        raise ValueError(f"Gateway {out['gw']} isn't on the same network as {out['ip']}")
    return out


def usb_line(cmd: str, net: dict | None = None) -> bytes:
    """The command line for the USB port. A leading newline ends any noise the box may have buffered."""
    line = "@SB " + cmd
    if net is not None:
        line += "".join(f" {k}={net.get(k, '')}" for k in ("host", "ip", "mask", "gw", "dns"))
    return ("\n" + line + "\n").encode("ascii")


# --------------------------------------------------------------------------- USB replies
class ReplySplitter:
    """feed(bytes) -> (console bytes, [reply dicts]). Holds back only what could be the start of a reply."""

    def __init__(self):
        self.buf = b""

    def feed(self, data: bytes) -> tuple[bytes, list[dict]]:
        self.buf += data
        out, replies = b"", []
        while True:
            i = self.buf.find(REPLY_START)
            if i < 0:
                keep = _prefix_overlap(self.buf, REPLY_START)
                out += self.buf[: len(self.buf) - keep]
                self.buf = self.buf[len(self.buf) - keep:]
                return out, replies
            j = self.buf.find(REPLY_END, i + len(REPLY_START))
            if j < 0:
                if len(self.buf) - i > 4096:  # not a real reply: give the bytes back
                    out += self.buf[: i + 1]
                    self.buf = self.buf[i + 1:]
                    continue
                out += self.buf[:i]
                self.buf = self.buf[i:]
                return out, replies
            body = self.buf[i + len(REPLY_START):j]
            out += self.buf[:i]
            self.buf = self.buf[j + len(REPLY_END):]
            try:
                r = json.loads(body.decode("utf-8", "replace"))
                if isinstance(r, dict):
                    replies.append(r)
            except ValueError:
                pass

    def flush(self) -> bytes:
        b, self.buf = self.buf, b""
        return b


def _prefix_overlap(buf: bytes, marker: bytes) -> int:
    for n in range(min(len(marker) - 1, len(buf)), 0, -1):
        if buf.endswith(marker[:n]):
            return n
    return 0


def _reply(r: dict) -> dict:
    if not r.get("ok"):
        raise BridgeError(r.get("error") or "the ScoreBridge refused the settings")
    return r


def usb_request(port: str, line: bytes, timeout: float = 4.0, serial_factory=None) -> dict:
    """Open the USB port, send one command, return the reply. For when ScoreReader isn't already reading it."""
    if serial_factory is None:
        import serial

        def serial_factory():
            s = serial.Serial()
            s.port, s.baudrate, s.timeout = port, USB_BAUD, 0.2
            s.dtr = s.rts = False  # both off: the auto-reset circuit leaves the ESP32 running
            s.open()
            return s
    try:
        ser = serial_factory()
    except Exception as e:
        raise BridgeError(f"can't open {port}: {e}") from None
    try:
        return _reply(exchange(ser.write, ser.read, line, timeout))
    finally:
        ser.close()


def exchange(write, read, line: bytes, timeout: float, on_data=None) -> dict:
    """Send `line` (again every 1.5 s) and read until a reply arrives. on_data gets the console bytes."""
    sp = ReplySplitter()
    end, next_send = time.time() + timeout, 0.0
    while time.time() < end:
        if time.time() >= next_send:
            write(line)
            next_send = time.time() + 1.5
        data, replies = sp.feed(read(4096) or b"")
        if data and on_data:
            on_data(data)
        if replies:
            rest = sp.flush()
            if rest and on_data:
                on_data(rest)
            return replies[0]
    if on_data:
        rest = sp.flush()
        if rest:
            on_data(rest)
    raise BridgeError("no answer on USB: is it a ScoreBridge with firmware 1.1 or newer?")


# --------------------------------------------------------------------------- network
def _url(host: str) -> str:
    return f"http://{host}/api/network"


def http_get(host: str, timeout: float = 3.0) -> dict:
    try:
        with urllib.request.urlopen(_url(host), timeout=timeout) as r:
            j = json.loads(r.read(8192).decode("utf-8", "replace"))
    except Exception as e:
        raise BridgeError(f"can't read the ScoreBridge at {host}: {e} (firmware 1.1 or newer?)") from None
    if not isinstance(j, dict) or "dhcp" not in j:
        raise BridgeError(f"{host} didn't answer like a ScoreBridge")
    return {"ok": True, "network": j}


def http_set(host: str, net: dict, timeout: float = 4.0, password: str = "") -> dict:
    body = urllib.parse.urlencode(net).encode()
    req = urllib.request.Request(_url(host), data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    if password:
        import base64

        req.add_header("Authorization", "Basic " + base64.b64encode(f"admin:{password}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read(8192).decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise BridgeError("the ScoreBridge has a settings password: change it on its own page") from None
        try:
            j = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            raise BridgeError(f"the ScoreBridge said {e.code}") from None
    except Exception as e:
        raise BridgeError(f"can't reach the ScoreBridge at {host}: {e}") from None
    return _reply(j)


def new_address(net: dict) -> str:
    """Where to find the box after the change: its static IP, or name.local with DHCP."""
    return net["ip"] or f"{net['host']}.local"


def wait_for(addr: str, timeout: float = 25.0, status=None) -> bool:
    """True once the box answers at addr (it restarts after a network change)."""
    if status is None:
        from discovery import bridge_status as status
    end = time.time() + timeout
    time.sleep(2)
    while time.time() < end:
        if status(addr, timeout=1.5):
            return True
        time.sleep(1)
    return False
