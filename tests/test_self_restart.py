"""Settings that used to need a restart (MQTT, dashboard port) now apply in place."""
import json
import socket
import time
import urllib.request

import pytest

import scorereader as sr
from sportzcast_mqtt import MiniBroker, MqttPublisherClient


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "CONFIG_PATH", str(tmp_path / "scorereader_config.json"))
    monkeypatch.setattr(sr, "STATE_PATH", str(tmp_path / "state.bin"))
    monkeypatch.setattr(sr, "LOG_DIR", str(tmp_path / "logs"))
    return tmp_path


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def listening(port, tries=60):
    for _ in range(tries):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def closed(port, tries=60):
    for _ in range(tries):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                pass
        except OSError:
            return True
        time.sleep(0.05)
    return False


def make_app(over):
    saved = sr.deep_merge(sr.DEFAULT_CONFIG, over)
    return sr.App(json.loads(json.dumps(saved)), saved=saved)


def test_no_setting_needs_a_restart_any_more():
    assert not [p for p, spec in sr.SETTINGS.items() if spec[3] == "restart"]


def test_broker_stop_frees_its_ports():
    tp, wp = free_port(), free_port()
    b = MiniBroker("127.0.0.1", tp, wp, log=lambda *a: None)
    b.start()
    assert listening(tp) and listening(wp)
    c = socket.create_connection(("127.0.0.1", tp))
    for _ in range(40):  # wait until the broker has picked the connection up
        if b._writers:
            break
        time.sleep(0.05)
    b.stop()
    assert b.status == "stopped" and closed(tp) and closed(wp)
    c.settimeout(2)
    assert c.recv(10) == b""  # the client was disconnected
    c.close()
    b.publish("x", "y")  # no error after stop


def test_client_stop():
    c = MqttPublisherClient("127.0.0.1", free_port(), log=lambda *a: None)
    c.stop()
    c.publish("t", "p")
    assert c.status == "stopped" and c.sock is None


def test_mqtt_settings_apply_without_restart(paths):
    p1, p2, ws = free_port(), free_port(), free_port()
    app = make_app({"mqtt": {"enabled": False, "bind": "127.0.0.1", "tcp_port": p1, "ws_port": ws}, "source": "demo"})
    app.running = True
    try:
        assert app.apply_settings({"mqtt.enabled": True})["ok"]
        assert app.mqtt is not None and listening(p1)
        assert app.reader.raw_hook == app.mqtt.raw
        r = app.apply_settings({"mqtt.tcp_port": p2})
        assert r["ok"] and r["restart_needed"] == []
        assert listening(p2) and closed(p1)
        assert app.apply_settings({"mqtt.enabled": False})["ok"]
        assert app.mqtt is None and app.reader.raw_hook is None and closed(p2) and closed(ws)
    finally:
        if app.mqtt:
            app.mqtt.stop()
        app.stop.set()


def test_dashboard_port_moves(paths):
    p1, p2 = free_port(), free_port()
    app = make_app({"mqtt": {"enabled": False}, "http_port": p1, "source": "demo"})
    app.running = True
    app.restart_http(app._bind_http(p1))
    try:
        assert listening(p1)
        r = json.load(urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{p1}/api/settings", data=json.dumps({"http_port": p2}).encode(),
            headers={"Content-Type": "application/json", "X-ScoreReader": "1"})))
        assert r["ok"] and r["moved_to_port"] == p2
        assert urllib.request.urlopen(f"http://127.0.0.1:{p2}/data.json").status == 200
        assert closed(p1)
        with open(sr.CONFIG_PATH, encoding="utf-8") as f:
            assert json.load(f)["http_port"] == p2
    finally:
        app._srv.shutdown()
        app.stop.set()


def test_dashboard_port_in_use_changes_nothing(paths):
    p1 = free_port()
    busy = socket.socket()
    busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    busy.bind(("0.0.0.0", 0))
    busy.listen()
    app = make_app({"mqtt": {"enabled": False}, "http_port": p1, "source": "demo"})
    app.running = True
    try:
        r = app.apply_settings({"http_port": busy.getsockname()[1]})
        assert not r["ok"] and "can't be used" in r["errors"]["http_port"]
        assert app.cfg["http_port"] == p1
    finally:
        busy.close()
        app.stop.set()
