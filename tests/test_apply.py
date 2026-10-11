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
