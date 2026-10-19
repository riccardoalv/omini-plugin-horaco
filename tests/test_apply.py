import pytest
from omini_sdk import Change, PluginError

from omini_horaco import parse
from omini_horaco.collect import apply, collect


def change(port="Port 5", value=False, kind="port.enabled"):
    return Change(device="1c:2a:a3:2f:57:f7", kind=kind, port=port, value=value)


def enabled(devices, port):
    return next(i.enabled for i in devices[0].interfaces if i.name == port)


def test_reads_whether_each_port_is_enabled(switch, cfg):
    switch.disabled = {2}
    sw = collect(cfg)
    assert enabled(sw, "Port 1") is True
    assert enabled(sw, "Port 2") is False


def test_disables_a_port_keeping_its_other_settings(switch, cfg):
    devices = apply(cfg, change())
    # The port's form, as the browser posts it: duplex and speed stay as
    # configured (Auto), flow control Off.
    assert switch.forms == [
        {"port4": "on", "state": "0", "duplex": "2", "speed": "10", "flowctrl": "0", "cmd": "port"}
    ]
    assert switch.saved == 1  # saved only after the page shows it disabled
    assert enabled(devices, "Port 5") is False


def test_the_sfp_port_has_a_form_of_its_own(switch, cfg):
    apply(cfg, change("Port 9"))
    assert switch.forms[0]["port8"] == "on"
    apply(cfg, change("Port 9", True))
    assert switch.forms[1]["state"] == "1"
    assert switch.disabled == set()


def test_not_saved_when_the_switch_did_not_take_it(switch, cfg):
    switch.ignore_changes = True
    with pytest.raises(PluginError, match="did not apply"):
        apply(cfg, change())
    assert switch.saved == 0


def test_nothing_is_posted_when_the_port_is_already_so(switch, cfg):
    apply(cfg, change(value=True))
    assert switch.forms == [] and switch.saved == 0


def test_unknown_ports_and_changes(switch, cfg):
    with pytest.raises(PluginError, match="no Port 12"):
        apply(cfg, change("Port 12"))
    with pytest.raises(PluginError, match="cannot apply"):
        apply(cfg, change(value="off"))
    assert switch.forms == []


def test_a_setting_the_form_does_not_offer_changes_nothing(switch, cfg):
    switch.pages["port.cgi"] = switch.pages["port.cgi"].replace(
        "<td>Port 5</td><td>Enable</td><td>Auto</td>",
        "<td>Port 5</td><td>Enable</td><td>Forced</td>",
    )
    with pytest.raises(PluginError, match="nothing was changed"):
        apply(cfg, change())
    assert switch.forms == []


def test_port_forms_follow_their_labels():
    from pathlib import Path

    html = (Path(__file__).parent / "fixtures" / "port.cgi.html").read_text()
    forms = parse.parse_port_forms(html)
    assert sorted(forms) == list(range(1, 10))
    assert forms[1].checkbox == "port0" and forms[9].checkbox == "port8"
    assert forms[9].options["speed"]["10g"] == "4"
    assert "10g" not in forms[1].options["speed"]
    assert forms[1].options["state"] == {"enable": "1", "disable": "0"}


def vlans_of(devices, port):
    return next(i.vlans for i in devices[0].interfaces if i.name == port)


def test_reads_the_vlans(switch, cfg):
    [sw] = collect(cfg)
    assert [(v.id, v.name) for v in sw.vlans] == [(1, None), (10, "Office"), (20, "Guests")]
    p1, p9 = vlans_of([sw], "Port 1"), vlans_of([sw], "Port 9")
    assert p1.model_dump(exclude_none=True) == {"untagged": 1}  # access
    assert p9.model_dump() == {"untagged": 1, "tagged": [10, 20]}  # trunk: native 1


def test_puts_a_port_in_a_vlan(switch, cfg):
    devices = apply(cfg, change("Port 5", {"untagged": 20}, "port.vlans"))
    assert switch.forms[-1] == {
        "port4": "on",
        "vlan_type": "0",
        "accept_type": "0",
        "vlan_access": "20",
    }
    assert switch.saved == 1
    assert vlans_of(devices, "Port 5").untagged == 20


def test_makes_a_port_a_trunk(switch, cfg):
    devices = apply(cfg, change("Port 3", {"untagged": 1, "tagged": [20]}, "port.vlans"))
    form = switch.forms[-1]
    assert (form["vlan_type"], form["vlan_native"], form["VLAN1"], form["VLAN20"]) == (
        "1",
        "1",
        "on",
        "on",
    )
    assert "VLAN10" not in form
    assert vlans_of(devices, "Port 3").model_dump() == {"untagged": 1, "tagged": [20]}


def test_port_vlans_refusals(switch, cfg):
    with pytest.raises(PluginError, match="native VLAN"):
        apply(cfg, change("Port 3", {"tagged": [10, 20]}, "port.vlans"))
    with pytest.raises(PluginError, match="no VLAN 30"):
        apply(cfg, change("Port 3", {"untagged": 30}, "port.vlans"))
    switch.ignore_changes = True
    with pytest.raises(PluginError, match="did not apply"):
        apply(cfg, change("Port 3", {"untagged": 10}, "port.vlans"))
    assert switch.saved == 0


def test_creates_and_renames_vlans(switch, cfg):
    sw = apply(cfg, change(None, {"id": 30, "name": "Cameras of the warehouse"}, "vlan.set"))[0]
    assert switch.forms[-1] == {"vid": "30", "name": "Cameras of the"}  # 14 characters at most
    assert (30, "Cameras of the") in [(v.id, v.name) for v in sw.vlans]
    sw = apply(cfg, change(None, {"id": 10, "name": "Staff"}, "vlan.set"))[0]
    assert (10, "Staff") in [(v.id, v.name) for v in sw.vlans]
    assert switch.saved == 2
    before = len(switch.forms)
    apply(cfg, change(None, {"id": 10, "name": "Staff"}, "vlan.set"))
    assert len(switch.forms) == before  # already so


def test_the_native_vlan_listed_as_tagged_is_dropped(switch, cfg):
    change_ = change("Port 3", {"untagged": 1, "tagged": [1, 20]}, "port.vlans")
    devices = apply(cfg, change_)
    assert switch.saved == 1
    assert vlans_of(devices, "Port 3").model_dump() == {"untagged": 1, "tagged": [20]}


def test_a_port_change_not_taken_is_undone(switch, cfg):
    switch.ignore_changes = True
    with pytest.raises(
        PluginError,
        match=r"did not apply the change to Port 5, not saved: it was not seen within 30 s "
        r"\(undone\)$",
    ):
        apply(cfg, change())
    # The change, then the port's settings as they were (enabled).
    assert [f["state"] for f in switch.forms] == ["0", "1"]
    assert switch.forms[-1]["port4"] == "on" and switch.saved == 0


def test_port_vlans_not_taken_are_undone(switch, cfg):
    switch.ignore_changes = True
    with pytest.raises(PluginError, match=r"not saved: .* \(undone\)$"):
        apply(cfg, change("Port 9", {"untagged": 10}, "port.vlans"))
    # Back to the trunk it was: native 1, VLANs 1, 10 and 20.
    restored = switch.forms[-1]
    assert (restored["port8"], restored["vlan_type"], restored["vlan_native"]) == ("on", "1", "1")
    assert {k for k in restored if k.startswith("VLAN")} == {"VLAN1", "VLAN10", "VLAN20"}
    assert switch.saved == 0


def test_a_rename_not_taken_is_undone(switch, cfg):
    switch.ignore_changes = True
    with pytest.raises(PluginError, match=r"not saved: .* \(undone\)$"):
        apply(cfg, change(None, {"id": 10, "name": "Staff"}, "vlan.set"))
    assert switch.forms[-1] == {"vid": "10", "name": "Office"}
    forms = len(switch.forms)
    with pytest.raises(PluginError, match="not saved"):
        apply(cfg, change(None, {"id": 30, "name": "New"}, "vlan.set"))  # nothing to undo
    assert len(switch.forms) == forms + 1  # nothing posted back
    assert switch.saved == 0


def test_a_trunk_of_every_vlan_carries_those_that_exist(switch, cfg):
    switch.pages["vlan.cgi_page-port_based"] = switch.pages["vlan.cgi_page-port_based"].replace(
        "<td>1,10,20</td>", "<td>1-4094</td>"
    )
    [sw] = collect(cfg)
    assert vlans_of([sw], "Port 9").model_dump() == {"untagged": 1, "tagged": [10, 20]}
    apply(cfg, change("Port 9", {"untagged": 1, "tagged": [10, 20]}, "port.vlans"))
    assert switch.forms == []  # already so


def test_a_change_shown_a_moment_later_is_saved(switch, cfg):
    # The switch shows the change on the second read: read again, then saved.
    switch.ignore_changes = True
    inner = switch.handler
    reads = {"n": 0}

    def handler(request):
        if request.method == "GET" and request.url.path == "/port.cgi" and switch.forms:
            reads["n"] += 1
            if reads["n"] == 2:
                switch.disabled.add(5)
        return inner(request)

    import httpx

    import omini_horaco.collect as collect_module

    collect_module.Client.keywords["transport"] = httpx.MockTransport(handler)
    apply(cfg, change())
    assert switch.saved == 1 and [f["state"] for f in switch.forms] == ["0"]


def test_an_undo_that_fails_says_so(switch, cfg):
    import httpx

    import omini_horaco.collect as collect_module

    switch.ignore_changes = True
    inner = switch.handler

    def handler(request):
        if request.method == "POST" and switch.forms:  # the undo is refused
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return inner(request)

    collect_module.Client.keywords["transport"] = httpx.MockTransport(handler)
    with pytest.raises(PluginError, match=r"\(could not be undone: check it on the switch\)$"):
        apply(cfg, change())
    assert switch.saved == 0
