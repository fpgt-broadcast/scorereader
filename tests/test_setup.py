"""First-run setup: device discovery, the setup_done flag, the webhook output and output status."""
import json
import socket
import struct
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import discovery
import scorereader as sr


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "CONFIG_PATH", str(tmp_path / "scorereader_config.json"))
    monkeypatch.setattr(sr, "STATE_PATH", str(tmp_path / "state.bin"))
    monkeypatch.setattr(sr, "LOG_DIR", str(tmp_path / "logs"))
    return tmp_path


def make_app(saved_over=None):
    saved = sr.deep_merge(sr.DEFAULT_CONFIG, {"mqtt": {"enabled": False}, **(saved_over or {})})
    return sr.App(json.loads(json.dumps(saved)), saved=saved)


# ---------------------------------------------------------------- setup_done
def test_new_config_starts_the_setup(paths):
    assert sr.load_config()["setup_done"] is False


def test_config_from_an_older_version_counts_as_set_up(paths):
    with open(sr.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"source": "tcp", "tcp": {"host": "10.0.0.5", "port": 10001}}, f)
    cfg = sr.load_config()
    assert cfg["setup_done"] is True and cfg["tcp"]["host"] == "10.0.0.5"


def test_finishing_the_setup_is_saved(paths):
    app = make_app()
    assert app.apply_settings({"setup_done": True})["ok"]
    with open(sr.CONFIG_PATH, encoding="utf-8") as f:
        assert json.load(f)["setup_done"] is True


# ---------------------------------------------------------------- mDNS
def _name(n):
    return b"".join(bytes([len(p)]) + p.encode() for p in n.split(".")) + b"\0"


def _rr(name, rtype, rdata):
    return name + struct.pack(">HHIH", rtype, 1, 120, len(rdata)) + rdata


def test_mdns_answer_is_parsed():
    inst = _name("scorebridge._scorebridge._tcp.local")
    host = _name("scorebridge.local")
    pkt = struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 2)
    pkt += _rr(_name("_scorebridge._tcp.local"), 12, inst)
    pkt += _rr(inst, 33, struct.pack(">HHH", 0, 0, 10001) + host)
    pkt += _rr(host, 1, socket.inet_aton("192.168.50.40"))
    r = discovery.parse_response(pkt)
    assert r["ptr"] == ["scorebridge._scorebridge._tcp.local"]
    assert r["srv"]["scorebridge._scorebridge._tcp.local"] == ("scorebridge.local", 10001)
    assert r["a"]["scorebridge.local"] == "192.168.50.40"


def test_mdns_compressed_names_and_garbage():
    # answer name compressed to the question at offset 12
    q = _name("_scorebridge._tcp.local")
    pkt = struct.pack(">HHHHHH", 0, 0x8400, 1, 1, 0, 0) + q + struct.pack(">HH", 12, 1)
    pkt += b"\xc0\x0c" + struct.pack(">HHIH", 12, 1, 120, 2) + b"\xc0\x0c"
    assert discovery.parse_response(pkt)["ptr"] == ["_scorebridge._tcp.local"]
    assert discovery.parse_response(b"\x00" * 5) == {"ptr": [], "srv": {}, "a": {}}
    assert discovery.parse_response(pkt[:-3])["ptr"] == []  # cut short: no crash
    assert discovery.build_query().endswith(struct.pack(">HH", 12, 0x8001))


# ---------------------------------------------------------------- discovery
STATUS = {"version": "1.0", "tcp_port": 10001, "recent_hex": "16 01 30", "last_byte_ms": 120}


def test_bridge_found_by_mdns_is_first(monkeypatch):
    monkeypatch.setattr(discovery, "mdns_browse",
                        lambda timeout=1.5: [{"name": "scorebridge", "host": "scorebridge.local", "ip": "10.1.1.9", "port": 10001}])
    monkeypatch.setattr(discovery, "_resolve", lambda n, timeout=1.5: "10.1.1.9")
    monkeypatch.setattr(discovery, "bridge_status", lambda ip, timeout=1.2: dict(STATUS))
    monkeypatch.setattr(discovery, "scan_subnet", lambda **k: pytest.fail("no scan when mDNS works"))
    monkeypatch.setattr(discovery, "find_usb", lambda ports=None: [{"kind": "scorelink", "title": "ScoreBot"}])
    found = discovery.discover()
    assert [d["kind"] for d in found] == ["scorebridge", "scorelink"]
    b = found[0]
    assert b["receiving"] and b["settings"] == {"source": "tcp", "tcp.host": "scorebridge.local", "tcp.port": 10001}


def test_scan_fallback_uses_the_ip(monkeypatch):
    monkeypatch.setattr(discovery, "mdns_browse", lambda timeout=1.5: [])
    monkeypatch.setattr(discovery, "_resolve", lambda n, timeout=1.5: None)
    monkeypatch.setattr(discovery, "scan_subnet", lambda **k: ["10.1.1.20", "10.1.1.21"])
    # .21 has port 10001 open but isn't a ScoreBridge (a Lantronix box, say)
    monkeypatch.setattr(discovery, "bridge_status",
                        lambda ip, timeout=1.2: {**STATUS, "recent_hex": ""} if ip == "10.1.1.20" else None)
    found = discovery.find_bridges()
    assert len(found) == 1 and found[0]["settings"]["tcp.host"] == "10.1.1.20" and not found[0]["receiving"]
    assert "no data from the console yet" in found[0]["detail"]


def test_usb_devices_are_classified():
    P = lambda dev, vid, pid, desc: SimpleNamespace(device=dev, vid=vid, pid=pid, description=desc,
                                                    manufacturer="", product="")
    found = discovery.find_usb([P("/dev/ttyUSB0", 0x1A86, 0x7523, "USB Serial"), P("COM7", 0x04D8, 0x00DD, "MCP2221"),
                                P("/dev/cu.Bluetooth-Incoming-Port", None, None, "n/a"),
                                P("/dev/ttyACM1", 0x2341, 0x0043, "Arduino Uno")])
    assert [d["kind"] for d in found] == ["scorelink", "scorebridge_usb", "serial"]
    assert found[1]["settings"] == {"source": "serial", "port": "/dev/ttyUSB0", "baud": 115200}


def test_real_bridge_status_over_http(monkeypatch):
    """bridge_status talks plain HTTP: point it at a local fake ScoreBridge."""
    class Fake(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = json.dumps(STATUS).encode() if self.path == "/api/status" else b"no"
            self.send_response(200 if self.path == "/api/status" else 404)
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    real = urllib.request.urlopen
    port = srv.server_address[1]
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, timeout=None: real(url.replace("127.0.0.1/", f"127.0.0.1:{port}/"), timeout=timeout))
    try:
        assert discovery.bridge_status("127.0.0.1")["tcp_port"] == 10001
    finally:
        srv.shutdown()


# ---------------------------------------------------------------- webhook
def test_webhook_url_is_checked(paths):
    app = make_app()
    res = app.apply_settings({"webhook.url": "ftp://x"})
    assert not res["ok"] and "http" in res["errors"]["webhook.url"]
    assert app.apply_settings({"webhook.url": ""})["ok"]


def test_webhook_posts_on_change(paths):
    got = []

    class Sink(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers["Content-Length"])
            got.append(json.loads(self.rfile.read(n)))
            self.send_response(204)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    app = make_app()
    app.running = True
    threading.Thread(target=app.update_loop, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/hook"
        assert app.apply_settings({"webhook.url": url, "webhook.enabled": True, "webhook.min_interval_ms": 50})["ok"]
        for _ in range(100):
            if got:
                break
            time.sleep(0.05)
        assert got and got[0]["home_score"] == "0" and got[0]["home_name"] == "HOME"
        assert app.outputs_json()["webhook"]["state"].startswith("ok")
        app.apply_settings({"teams.home_name": "EAGLES"})
        for _ in range(100):
            if got[-1].get("home_name") == "EAGLES":
                break
            time.sleep(0.05)
        assert got[-1]["home_name"] == "EAGLES"
        assert app.apply_settings({"webhook.enabled": False})["ok"] and app.webhook is None
    finally:
        app.stop.set()
        srv.shutdown()


# ---------------------------------------------------------------- output status
def test_reads_are_tracked(paths):
    app = make_app()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        urllib.request.urlopen(base + "/data.xml").read()
        st = json.load(urllib.request.urlopen(base + "/api/state"))
        assert st["outputs"]["xml"]["age"] < 5 and st["outputs"]["xml"]["by"] == "127.0.0.1"
        assert "setup_done" in st and st["outputs"]["mqtt"]["enabled"] is False
        assert "Setup" in urllib.request.urlopen(base + "/").read().decode()
    finally:
        srv.shutdown()
