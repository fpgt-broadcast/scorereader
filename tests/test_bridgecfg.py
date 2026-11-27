"""ScoreBridge network settings: checks, the USB reply framing, a fake box over HTTP and USB, and the App glue."""
import json
import threading
import urllib.parse
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import bridgecfg as bc
import scorereader as sr


# ---------------------------------------------------------------- checks
def test_dhcp_and_static_are_checked():
    assert bc.clean_network({"dhcp": True, "hostname": "scorebridge"}) == \
        {"host": "scorebridge", "ip": "", "mask": "", "gw": "", "dns": ""}
    n = bc.clean_network({"dhcp": False, "ip": "192.168.50.40", "gateway": "192.168.50.1"})
    assert n == {"host": "scorebridge", "ip": "192.168.50.40", "mask": "255.255.255.0", "gw": "192.168.50.1", "dns": ""}
    for bad, word in [({"dhcp": False, "ip": ""}, "Type the IP"), ({"dhcp": False, "ip": "192.168.1.300"}, "not an IPv4"),
                      ({"dhcp": False, "ip": "10.0.0.5", "mask": "255.0.255.0"}, "Subnet mask"),
                      ({"dhcp": False, "ip": "10.0.0.5", "gateway": "10.0.1.1", "mask": "255.255.255.0"}, "same network"),
                      ({"dhcp": False, "ip": "10.0.0.255"}, "can't be used"),
                      ({"dhcp": True, "hostname": "score bridge"}, "Name")]:
        with pytest.raises(ValueError, match=word):
            bc.clean_network(bad)


def test_usb_line():
    n = bc.clean_network({"dhcp": False, "ip": "10.0.0.5"})
    assert bc.usb_line("GET") == b"\n@SB GET\n"
    assert bc.usb_line("NET", n) == b"\n@SB NET host=scorebridge ip=10.0.0.5 mask=255.255.255.0 gw= dns=\n"


# ---------------------------------------------------------------- USB framing
def test_reply_is_taken_out_of_the_console_stream():
    console = bytes(range(256)) * 3
    reply = bc.REPLY_START + b'{"ok":true,"network":{"dhcp":true}}' + bc.REPLY_END
    stream = console[:300] + reply + console[300:]
    for size in (1, 5, 64, 4096):  # any chunking
        sp, out, got = bc.ReplySplitter(), b"", []
        for i in range(0, len(stream), size):
            d, r = sp.feed(stream[i:i + size])
            out += d
            got += r
        out += sp.flush()
        assert out == console and got == [{"ok": True, "network": {"dhcp": True}}]


def test_a_lone_marker_start_is_given_back():
    sp = bc.ReplySplitter()
    d, r = sp.feed(bc.REPLY_START + bytes(5000))
    assert not r and d.startswith(b"\r") and len(d) + len(sp.flush()) == len(bc.REPLY_START) + 5000


class FakeBox:
    """Answers the firmware's USB commands, with console bytes around the reply like the real box."""

    def __init__(self):
        self.net = {"hostname": "scorebridge", "dhcp": True, "ip": "", "mask": "", "gateway": "", "dns": "",
                    "tcp_port": 10001, "current_ip": "192.168.50.77", "link": True}
        self.out = bytearray()
        self.lines = []
        self.buf = b""
        self.closed = False

    def write(self, b):
        self.buf += b
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            line = line.decode().strip()
            if not line.startswith("@SB "):
                continue
            self.lines.append(line)
            words = line.split(" ")
            if words[1] == "GET":
                r = {"ok": True, "network": dict(self.net)}
            else:
                a = dict(w.split("=", 1) for w in words[2:])
                changed = a["ip"] != self.net["ip"]
                self.net.update(hostname=a["host"], ip=a["ip"], mask=a["mask"], gateway=a["gw"], dns=a["dns"],
                                dhcp=not a["ip"])
                r = {"ok": True, "restarting": changed, "network": dict(self.net)}
            self.out += b"\x16\x01CONSOLE" + bc.REPLY_START + json.dumps(r).encode() + bc.REPLY_END + b"MORE"

    def read(self, n):
        b, self.out[:] = bytes(self.out[:n]), self.out[n:]
        return b

    def close(self):
        self.closed = True


def test_usb_request_against_a_fake_box():
    box = FakeBox()
    r = bc.usb_request("COM7", bc.usb_line("GET"), serial_factory=lambda: box)
    assert r["network"]["current_ip"] == "192.168.50.77" and box.closed
    n = bc.clean_network({"dhcp": False, "ip": "192.168.50.40", "gateway": "192.168.50.1"})
    r = bc.usb_request("COM7", bc.usb_line("NET", n), serial_factory=lambda: box)
    assert r["restarting"] and box.net["ip"] == "192.168.50.40" and bc.new_address(n) == "192.168.50.40"
    assert bc.new_address(bc.clean_network({"dhcp": True, "hostname": "gym"})) == "gym.local"


def test_console_bytes_still_reach_the_decoders():
    box, seen = FakeBox(), bytearray()
    bc.exchange(box.write, box.read, bc.usb_line("GET"), 2, on_data=seen.extend)
    assert bytes(seen) == b"\x16\x01CONSOLEMORE"


def test_no_answer_on_usb():
    with pytest.raises(bc.BridgeError, match="firmware 1.1"):
        bc.exchange(lambda b: None, lambda n: b"\x00" * 10, bc.usb_line("GET"), 0.3)


# ---------------------------------------------------------------- over the network
@pytest.fixture
def http_box():
    state = {"net": {"hostname": "scorebridge", "dhcp": True, "ip": "", "mask": "", "gateway": "", "dns": "",
                     "tcp_port": 10001, "current_ip": "127.0.0.1", "link": True}, "posts": []}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._json(200, state["net"])

        def do_POST(self):
            a = {k: v[0] for k, v in urllib.parse.parse_qs(
                self.rfile.read(int(self.headers["Content-Length"])).decode(), keep_blank_values=True).items()}
            state["posts"].append(a)
            if a.get("ip") == "1.2.3.4":
                return self._json(400, {"ok": False, "error": "IP address is not valid"})
            changed = a["ip"] != state["net"]["ip"]
            state["net"].update(ip=a["ip"], dhcp=not a["ip"], hostname=a["host"])
            self._json(200, {"ok": True, "restarting": changed, "network": state["net"]})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state["host"] = f"127.0.0.1:{srv.server_address[1]}"
    yield state
    srv.shutdown()


def test_http_get_and_set(http_box):
    assert bc.http_get(http_box["host"])["network"]["dhcp"] is True
    r = bc.http_set(http_box["host"], bc.clean_network({"dhcp": False, "ip": "10.9.8.7"}))
    assert r["restarting"] and http_box["posts"][-1]["mask"] == "255.255.255.0"
    with pytest.raises(bc.BridgeError, match="not valid"):
        bc.http_set(http_box["host"], {"host": "x", "ip": "1.2.3.4", "mask": "", "gw": "", "dns": ""})


# ---------------------------------------------------------------- App
@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "CONFIG_PATH", str(tmp_path / "scorereader_config.json"))
    monkeypatch.setattr(sr, "STATE_PATH", str(tmp_path / "state.bin"))
    monkeypatch.setattr(sr, "LOG_DIR", str(tmp_path / "logs"))
    return tmp_path


def make_app(over):
    saved = sr.deep_merge(sr.DEFAULT_CONFIG, {"mqtt": {"enabled": False}, **over})
    return sr.App(json.loads(json.dumps(saved)), saved=saved)


def test_app_follows_the_box_to_its_new_address(paths, http_box):
    app = make_app({"source": "tcp", "tcp": {"host": http_box["host"], "port": 10001}})
    r = app.bridge_network({})
    assert r["ok"] and r["via"] == "net" and r["network"]["current_ip"] == "127.0.0.1"
    r = app.bridge_network({}, {"dhcp": False, "ip": "10.9.8.7"})
    assert r["ok"] and r["followed"] == "10.9.8.7" and app.cfg["tcp"]["host"] == "10.9.8.7"
    with open(sr.CONFIG_PATH, encoding="utf-8") as f:
        assert json.load(f)["tcp"]["host"] == "10.9.8.7"
    app.stop.set()


def test_app_errors_are_plain(paths):
    app = make_app({"source": "demo"})
    assert "USB or the network" in app.bridge_network({})["error"]
    assert "IP" in app.bridge_network({"via": "net", "host": "x"}, {"dhcp": False, "ip": ""})["error"]
    assert "USB port" in app.bridge_network({"via": "usb"})["error"]


def test_app_uses_the_open_usb_port(paths, monkeypatch):
    """With the serial source on that port, the command goes through the reader (the port is already open)."""
    app = make_app({"source": "serial", "port": "COM7"})
    box = FakeBox()
    rd = app.reader
    rd.serial_port = "COM7"
    monkeypatch.setattr(rd, "is_alive", lambda: True)
    monkeypatch.setattr(bc, "usb_request", lambda *a, **k: pytest.fail("must not open the port twice"))

    def serve():
        while rd.usb_req is None:
            pass
        rd._serve_usb_req(box)
    threading.Thread(target=serve, daemon=True).start()
    r = app.bridge_network({}, {"dhcp": True, "hostname": "gym"})
    assert r["ok"] and r["via"] == "usb" and r["address"] == "gym.local" and box.net["hostname"] == "gym"
    app.stop.set()


def test_http_routes(paths, http_box):
    app = make_app({"source": "tcp", "tcp": {"host": http_box["host"], "port": 10001}})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        get = urllib.request.Request(base + "/api/bridge/network", headers={"X-ScoreReader": "1"})
        assert json.load(urllib.request.urlopen(get))["ok"]
        try:  # without the dashboard's header (a link on another site) it does nothing
            urllib.request.urlopen(base + "/api/bridge/network")
            raise AssertionError("expected 403")
        except urllib.error.HTTPError as e:
            assert e.code == 403
        req = urllib.request.Request(base + "/api/bridge/network", method="POST",
                                     data=json.dumps({"dhcp": True, "hostname": "gym"}).encode(),
                                     headers={"X-ScoreReader": "1", "Content-Type": "application/json"})
        r = json.load(urllib.request.urlopen(req))
        assert r["ok"] and http_box["posts"][-1]["host"] == "gym"
        assert "ScoreBridge network" in urllib.request.urlopen(base + "/").read().decode()
    finally:
        srv.shutdown()
        app.stop.set()
