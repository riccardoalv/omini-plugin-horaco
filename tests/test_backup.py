import json

import httpx
import pytest
from omini_sdk import ConfigBackup, PluginError

import omini_horaco.collect as collect_module
from omini_horaco.collect import backup, collect


def settings(cfg):
    [b] = backup(cfg)
    assert isinstance(b, ConfigBackup) and b.format == "json"
    return b, json.loads(b.content)


def test_one_backup_keyed_like_the_device(switch, cfg):
    b, s = settings(cfg)
    assert b.device == collect(cfg)[0].key
    assert s["system"] == {
        "name": "HC-SWTGW218AS",
        "ip": "192.168.1.96",
        "netmask": "255.255.255.0",
    }
    assert s["ports"]["Port 1"] == {
        "enabled": True,
        "duplex": "Auto",
        "speed": "Auto",
        "flow_control": "Off",
    }
    assert s["vlan_manage"] is True
    assert s["vlans"] == [
        {"id": 1, "name": ""},
        {"id": 10, "name": "Office"},
        {"id": 20, "name": "Guests"},
    ]
    assert s["port_vlans"]["Port 9"] == {
        "mode": "trunk",
        "access": 1,
        "native": 1,
        "trunk": [1, 10, 20],
    }
    assert s["port_vlans"]["Port 1"] == {"mode": "access", "access": 1, "native": None, "trunk": []}
    assert not {"uptime", "firmware", "mac"} & set(s["system"])  # status, not configuration


def test_only_reads(switch, cfg):
    backup(cfg)
    assert all(not p.startswith(("/port.cgi", "/vlan.cgi", "/save.cgi")) for p in switch.posts)
    assert not any("mac.cgi" in c or "stats" in c for c in switch.calls)
    assert "secret" not in backup(cfg)[0].content


def test_backups_are_deterministic(switch, cfg):
    first = backup(cfg)
    assert backup(cfg) == first
    switch.disabled = {3}
    after = backup(cfg)
    assert after != first
    assert json.loads(after[0].content)["ports"]["Port 3"]["enabled"] is False
    switch.disabled = set()
    switch.vlan_names = {20: "IoT"}  # renamed
    assert {"id": 20, "name": "IoT"} in json.loads(backup(cfg)[0].content)["vlans"]


def test_a_page_that_does_not_answer_fails_the_backup(switch, cfg):
    inner = switch.handler

    def handler(request):
        if request.url.path == "/vlan.cgi":
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return inner(request)

    collect_module.Client.keywords["transport"] = httpx.MockTransport(handler)
    # A backup without the VLANs would look like a change: none is made.
    with pytest.raises(PluginError, match=r"http://192\.168\.1\.96 failed: Server disconnected"):
        backup(cfg)


def test_the_plugin_registers_backup():
    import main

    assert main.plugin._backup is backup
