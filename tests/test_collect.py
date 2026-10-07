import pytest
from omini_sdk import PluginError

from omini_horaco import parse
from omini_horaco.collect import collect
from omini_horaco.collect import test as connection_test


def test_reads_the_switch(switch, cfg):
    [sw] = collect(cfg)
    assert sw.key == "1c:2a:a3:50:2c:33"  # its MAC: merges with what the network scan found
    assert (sw.name, sw.role, sw.vendor, sw.model, sw.os_version) == (
        "HC-SWTGW218AS",
        "switch",
        "Horaco",
        "HC-SWTGW218AS",
        "V200.2.4",
    )
    assert sw.uptime_s == ((14 * 24 + 8) * 60 + 57) * 60 + 43
    assert sw.ips == ["192.168.1.96"] and sw.host == "192.168.1.96"


def test_ports_with_speed_connector_and_counters(switch, cfg):
    [sw] = collect(cfg)
    ports = {i.name: i for i in sw.interfaces}
    assert len(ports) == 9
    p1, p3, p5, p9 = ports["Port 1"], ports["Port 3"], ports["Port 5"], ports["Port 9"]
    assert (p1.up, p1.speed_mbps, p1.duplex, p1.connector) == (True, 1000, "full", "rj45")
    assert (p3.up, p3.speed_mbps, p3.duplex) == (False, None, None)
    assert p5.speed_mbps == 2500
    assert (p9.speed_mbps, p9.connector) == (10000, "sfp")
    # 64-bit byte counters split as "high-low".
    assert p1.rx_bytes == 57 * 2**32 + 4000784606 and p1.tx_bytes == 3711982697


def test_mac_table_across_pages(switch, cfg):
    [sw] = collect(cfg)
    assert len(sw.fdb) == 13  # 10 on page 1, 3 on page 2
    first = sw.fdb[0]
    assert (first.port, first.vlan) == ("Port 5", 1)
    assert {e.port for e in sw.fdb} <= {f"Port {n}" for n in range(1, 10)}


def test_a_dropped_page_never_fails_the_collection(switch, cfg):
    switch.drop_paging = True
    [sw] = collect(cfg)
    assert len(sw.interfaces) == 9  # everything else is there
    assert len(sw.fdb) == 10  # the MAC table's first page
    assert switch.posts.count("/mac.cgi goto") == 8  # two rounds of tries, with a pause


def test_paging_waits_out_a_busy_switch(switch, cfg):
    switch.drop_paging_times = 5  # the first round fails, the second gets through
    [sw] = collect(cfg)
    assert len(sw.fdb) == 13  # both pages


def test_read_only(switch, cfg):
    collect(cfg)
    # Posted: turning the MAC table's page, nothing else (the session cookie
    # is valid, so no login either).
    assert switch.posts == ["/mac.cgi goto"]


def test_only_page_navigation_is_posted():
    from omini_horaco.client import Client

    with pytest.raises(ValueError):
        Client("192.0.2.1", "a", "b").page("/mac.cgi?page=fwd_tbl", form={"cmd": "mactblclr"})


def test_keeps_the_last_pages(switch, cfg):
    collect(cfg)
    kept = sorted(p.name for p in (cfg.state_dir / "pages").iterdir())
    assert "info.cgi.html" in kept and "mac.cgi_page-fwd_tbl_pageidx-2.html" in kept


def test_signs_in_only_when_the_switch_asks(switch, cfg):
    switch.session = False  # e.g. after a reboot
    collect(cfg)
    assert switch.posts.count("/login.cgi") == 1
    switch.posts.clear()
    collect(cfg)  # the next collection reuses the session
    assert "/login.cgi" not in switch.posts


def test_signs_in_again_when_the_session_expires(switch, cfg):
    switch.expire_once = True
    [sw] = collect(cfg)
    assert sw.model == "HC-SWTGW218AS"
    assert switch.posts[:1] == ["/login.cgi"]  # only when the switch asked


def test_wrong_password(switch, cfg):
    cfg["password"] = "wrong"
    with pytest.raises(PluginError, match="rejected the username or password"):
        collect(cfg)


def test_connection_test(switch, cfg):
    assert connection_test(cfg) == "Connected to HC-SWTGW218AS V200.2.4"


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
    assert parse.parse_panel('<img src="/RJ45.png"><img src="/Fiber.png">', 2) == {
        1: "rj45",
        2: "sfp",
    }
    assert parse.parse_mac_table("<p>nothing</p>") == []
