import pytest
from omini_sdk import PluginError

from omini_horaco import parse
from omini_horaco.collect import collect
from omini_horaco.collect import test as connection_test


def test_reads_the_switch(switch, cfg):
    [sw] = collect(cfg)
    assert sw.key == "1c:2a:a3:00:33:44"
    assert (sw.name, sw.role, sw.vendor, sw.model, sw.os_version) == (
        "HC-SWTGW218AS",
        "switch",
        "Horaco",
        "HC-SWTGW218AS",
        "V1.9",
    )
    assert sw.uptime_s == ((3 * 24 + 14) * 60 + 22) * 60 + 8
    assert sw.ips == ["192.168.1.96"] and sw.host == "192.168.1.96"


def test_ports_with_speed_connector_and_counters(switch, cfg):
    [sw] = collect(cfg)
    ports = {i.name: i for i in sw.interfaces}
    assert len(ports) == 9
    p1, p2, p3, p9 = ports["Port 1"], ports["Port 2"], ports["Port 3"], ports["Port 9"]
    assert (p1.up, p1.speed_mbps, p1.duplex, p1.connector) == (True, 2500, "full", "rj45")
    assert (p2.up, p2.speed_mbps) == (False, None)
    assert p3.duplex == "half" and p3.speed_mbps == 100
    assert (p9.speed_mbps, p9.connector) == (10000, "sfp")
    # Packet counters only: no bytes are invented from them.
    assert p1.rx_bytes is None and p1.tx_bytes is None
    assert p1.rx_errors == 2 and p1.tx_errors == 0


def test_mac_table_across_pages(switch, cfg):
    [sw] = collect(cfg)
    fdb = [(e.mac, e.port, e.vlan) for e in sw.fdb]
    # The switch's own MAC (CPU port) is left out; duplicates across pages too.
    assert fdb == [
        ("58:9c:fc:10:8f:2c", "Port 9", 1),
        ("bc:24:11:aa:00:01", "Port 1", 1),
        ("bc:24:11:aa:00:02", "Port 3", 20),
    ]


def test_read_only(switch, cfg):
    collect(cfg)
    # The login form is the only thing ever posted.
    assert switch.posts == ["/login.cgi"]
    assert all(c.startswith("GET") for c in switch.calls if "login" not in c)


def test_keeps_the_last_pages(switch, cfg):
    collect(cfg)
    kept = sorted(p.name for p in (cfg.state_dir / "pages").iterdir())
    assert "info.cgi.html" in kept and "mac.cgi_page-fwd_tbl_pageidx-2.html" in kept


def test_signs_in_again_when_the_session_expires(switch, cfg):
    switch.expire_once = True
    [sw] = collect(cfg)
    assert sw.model == "HC-SWTGW218AS"
    assert switch.posts == ["/login.cgi", "/login.cgi"]


def test_wrong_password(switch, cfg):
    cfg["password"] = "wrong"
    with pytest.raises(PluginError, match="rejected the username or password"):
        collect(cfg)


def test_connection_test(switch, cfg):
    assert connection_test(cfg) == "Connected to HC-SWTGW218AS V1.9"


def test_missing_settings(cfg):
    cfg["password"] = ""
    with pytest.raises(PluginError, match="required"):
        collect(cfg)


@pytest.mark.parametrize(
    ("text", "mbps"),
    [
        ("1000M", 1000),
        ("2.5G", 2500),
        ("2500", 2500),
        ("10G", 10000),
        ("10GFull", 10000),
        ("", None),
    ],
)
def test_speeds(text, mbps):
    assert parse.speed_mbps(text) == mbps


def test_uptime_formats():
    assert parse.uptime_seconds("0Day1Hour0Minute5Second") == 3605
    assert parse.uptime_seconds("2 days 3 hours") == 2 * 86400 + 3 * 3600
    assert parse.uptime_seconds("12:00:01") == 43201
    assert parse.uptime_seconds("") is None


def test_unknown_layout_is_left_out():
    assert parse.parse_port_status("<table><tr><td>hello</td></tr></table>") == {}
    assert parse.parse_panel('<img src="/RJ45.png">', 9) == {}
    assert parse.parse_mac_table("<p>nothing</p>") == []
