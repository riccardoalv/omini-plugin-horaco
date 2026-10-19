"""Turns the switch's web pages into an Omini device."""

from __future__ import annotations

import json
import time

from omini_sdk import (
    Change,
    Config,
    ConfigBackup,
    Device,
    FdbEntry,
    Interface,
    PluginError,
    PortVlans,
    Vlan,
    log,
)
from omini_sdk import optional as sdk_optional
from omini_sdk.changes import apply_verify
from omini_sdk.http import settings

from omini_horaco import parse
from omini_horaco.client import Client, Unanswered

INFO = "/info.cgi"
STATS = "/port.cgi?page=stats"
PANEL = "/panel.cgi"
MACS = "/mac.cgi?page=fwd_tbl"
PORTS = "/port.cgi"
SAVE = "/save.cgi"
VLANS = "/vlan.cgi?page=static"
PORT_VLANS = "/vlan.cgi?page=port_based"
# VLAN names on these switches: at most this many characters.
VLAN_NAME_MAX = 14
# The MAC table is paged on this firmware; never read more than this many pages.
MAX_MAC_PAGES = 64
# How long a change waits for the switch's page to show it (the SDK's window).
VERIFY_S = 30
POLL_S = 2


def client_from(cfg: Config) -> Client:
    host, user, password = cfg.str("host"), cfg.str("username", "admin"), cfg.str("password")
    if not host or not user or not password:
        raise PluginError("the address, username and password are required")
    pages = cfg.state_dir / "pages" if cfg.state_dir else None
    return Client(host, user, password, timeout=settings(cfg)["timeout"], pages_dir=pages)


def optional(what: str, fn, default):
    """Optional pages: one failing never fails the collection (a wrong
    password does). A page the switch did not answer is logged once; anything
    else (most likely a page this parser does not understand) with its
    traceback, so it can be fixed."""
    return sdk_optional(what, fn, default)


# The switch sometimes refuses every connection for a few seconds (it closes
# them at once); one more round of tries after this pause usually gets through.
BUSY_PAUSE_S = 6


def page_patiently(c: Client, form: dict[str, str]) -> str:
    try:
        return c.page(MACS, form=form)
    except Unanswered:
        time.sleep(BUSY_PAUSE_S)
        return c.page(MACS, form=form)


def mac_table(c: Client) -> list[parse.FdbRow]:
    """All pages of the MAC table. The firmware turns pages with its form
    (cmd=goto), which changes nothing on the switch: it only picks the page."""
    first = c.page(MACS)
    out = parse.parse_mac_table(first)
    pages = min(parse.mac_pages(first), MAX_MAC_PAGES)
    per_page = parse.mac_per_page(first)
    for i in range(2, pages + 1):
        # The fields in the order the browser sends them.
        form = {"perpage": per_page} if per_page else {}
        form |= {"pageidx": str(i), "cmd": "goto"}
        try:
            out += parse.parse_mac_table(page_patiently(c, form))
        except Unanswered as e:
            # Keep the pages read: Omini remembers where the rest were.
            log.warning("MAC table: stopped at page %d of %d: %s", i, pages, e)
            break
    # Pages may overlap while the table changes: one entry per MAC and port.
    seen: set[tuple[str, int]] = set()
    unique = []
    for r in out:
        if (r.mac, r.port) not in seen:
            seen.add((r.mac, r.port))
            unique.append(r)
    return unique


def interfaces(
    ports: dict[int, parse.Port],
    config: dict[int, parse.PortConfig],
    vlans: dict[int, parse.PortVlanRow] | None = None,
) -> list[Interface]:
    out = []
    for n in sorted(ports):
        p = ports[n]
        c = config.get(n)
        out.append(
            Interface(
                name=parse.port_name(n),
                type="ethernet",
                connector=p.media,
                up=p.up,
                enabled=c.enabled if c else None,
                vlans=port_vlans((vlans or {}).get(n)),
                speed_mbps=p.speed_mbps,
                duplex=p.duplex,
                rx_bytes=p.rx_bytes,
                tx_bytes=p.tx_bytes,
                rx_errors=p.rx_errors,
                tx_errors=p.tx_errors,
            )
        )
    return out


def device_key(info: dict[str, str], c: Client) -> str:
    """The switch's MAC, else the address Omini reaches it at."""
    return parse.norm_mac(info.get("mac", "")) or c.base.split("://", 1)[-1]


def collect(cfg: Config) -> list[Device]:
    c = client_from(cfg)
    try:
        info_html = c.page(INFO)
        info = parse.parse_info(info_html)
        ports = parse.parse_port_status(info_html)
        port_html = optional("port settings", lambda: c.page(PORTS), "")
        if not ports:
            # Some models keep the status table on the port page.
            ports = parse.parse_port_status(port_html)
        config = parse.parse_port_config(port_html)
        optional("counters", lambda: parse.parse_stats(c.page(STATS), ports), None)
        media = optional("front panel", lambda: parse.parse_panel(c.page(PANEL), len(ports)), {})
        for n, kind in media.items():
            if n in ports:
                ports[n].media = kind
        fdb = optional("MAC table", lambda: mac_table(c), [])
        vlan_list = optional("VLANs", lambda: parse.parse_vlans(c.page(VLANS)), [])
        port_vlan_rows = existing_only(
            optional("port VLANs", lambda: parse.parse_port_vlans(c.page(PORT_VLANS)), {}),
            vlan_list,
        )

        mac = parse.norm_mac(info.get("mac", ""))
        host = c.base.split("://", 1)[-1]
        model = info.get("model")
        return [
            Device(
                key=device_key(info, c),
                name=info.get("name") or model or host,
                host=host,
                role="switch",
                vendor="Horaco" if model and model.upper().startswith("HC-") else None,
                model=model,
                os_version=info.get("firmware"),
                uptime_s=parse.uptime_seconds(info.get("uptime")),
                macs=[mac] if mac else None,
                ips=[info["ip"]] if info.get("ip") else [host],
                interfaces=interfaces(ports, config, port_vlan_rows) or None,
                vlans=[Vlan(id=i, name=name or None) for i, name in vlan_list] or None,
                fdb=[
                    FdbEntry(mac=r.mac, port=parse.port_name(r.port), vlan=r.vlan)
                    for r in fdb
                    if r.mac != mac
                ]
                or None,
            )
        ]
    finally:
        c.close()


def test(cfg: Config) -> str:
    c = client_from(cfg)
    try:
        info = parse.parse_info(c.page(INFO))
        if not info:
            raise PluginError("signed in, but this does not look like a supported switch")
        what = " ".join(v for v in (info.get("model"), info.get("firmware")) if v)
        return f"Connected to {what or c.base}"
    finally:
        c.close()


def apply(cfg: Config, change: Change) -> list[Device]:
    """Applies a change: a port enabled or disabled (port.enabled), a port's
    VLANs (port.vlans), a VLAN created or renamed (vlan.set). Each posts the
    page's form, reads the page again to check the switch took it, and only
    then saves the configuration (unsaved, a reboot undoes it). Returns the
    switch as read after the change."""
    if change.kind == "port.vlans":
        set_port_vlans(cfg, change)
        return collect(cfg)
    if change.kind == "vlan.set":
        set_vlan(cfg, change)
        return collect(cfg)
    if change.kind != "port.enabled" or not isinstance(change.value, bool):
        raise PluginError(f"this plugin cannot apply {change.kind}")
    n = parse.port_number(change.port or "")
    c = client_from(cfg)
    try:
        html = c.page(PORTS)
        current = parse.parse_port_config(html).get(n) if n else None
        form = parse.parse_port_forms(html).get(n) if n else None
        if current is None or form is None:
            raise PluginError(f"the switch's port settings have no {change.port}")
        if current.enabled != change.value:
            fields = port_form(form, current, change.value)
            # Built before the change: the port's settings as they were.
            previous = (
                port_form(form, current, current.enabled) if current.enabled is not None else None
            )

            def shown() -> bool:
                after = parse.parse_port_config(c.page(PORTS)).get(n)
                return after is not None and after.enabled == change.value

            change_and_save(
                c,
                PORTS,
                fields,
                shown,
                restore(c, PORTS, previous),
                f"the switch did not apply the change to {change.port}",
            )
    finally:
        c.close()
    return collect(cfg)


def restore(c: Client, path: str, previous: dict[str, str] | None):
    """The undo of a change the switch did not show as asked: posts the
    page's form back as it was, so its running configuration is not left half
    changed until a reboot. Raises when it cannot (the error then says to
    check it on the switch)."""

    def undo() -> None:
        if previous is None:
            raise PluginError(f"the settings of {path} before the change are unknown")
        c.submit(path, previous)

    return undo


def change_and_save(c: Client, path: str, form: dict[str, str], shown, undo, failure: str) -> None:
    """Posts a page's form (one request, all or nothing), reads the page
    again until it shows the change (every POLL_S seconds, up to VERIFY_S),
    and only then saves the configuration (unsaved, a reboot undoes it). A
    change not shown is undone and never saved."""
    apply_verify(
        lambda: c.submit(path, form),
        shown,
        undo,
        timeout=VERIFY_S,
        interval=POLL_S,
        message=f"{failure}, not saved",
        check_in="on the switch",
        undo_failed_apply=False,
        sleep=time.sleep,
    )
    c.submit(SAVE, {"cmd": "save"})


def port_form(form: parse.PortForm, current: parse.PortConfig, enabled: bool) -> dict[str, str]:
    """The fields the browser posts for one port, in its order: the port's
    other settings (duplex, speed, flow control) are sent as they are, never
    reset to the form's defaults."""

    def value(select: str, text: str) -> str:
        v = form.options.get(select, {}).get(" ".join(text.split()).lower())
        if v is None:
            raise PluginError(
                f"unknown {select} setting {text!r} on the switch: nothing was changed"
            )
        return v

    return {
        form.checkbox: "on",
        "state": value("state", "Enable" if enabled else "Disable"),
        "duplex": value("duplex", current.duplex),
        "speed": value("speed", current.speed),
        "flowctrl": value("flowctrl", current.flow),
        **form.hidden,
    }


def port_vlans(row: parse.PortVlanRow | None) -> PortVlans | None:
    """A port's VLANs as Omini knows them: access (untagged), trunk (the native
    VLAN untagged, the rest tagged). Hybrid ports are left out: Omini does not
    change them."""
    if row is None:
        return None
    if row.mode == "access" and row.access:
        return PortVlans(untagged=row.access)
    if row.mode == "trunk":
        tagged = [i for i in row.trunk if i != row.native]
        return PortVlans(untagged=row.native, tagged=tagged or None)
    return None


def existing_only(
    rows: dict[int, parse.PortVlanRow], vlan_list: list[tuple[int, str]]
) -> dict[int, parse.PortVlanRow]:
    """Keeps in each trunk only the VLANs the switch has: a trunk shown as
    "1-4094" allows every VLAN, but carries only those that exist. Left as
    read when the VLAN list is unknown."""
    ids = {i for i, _ in vlan_list}
    if ids:
        for r in rows.values():
            r.trunk = [i for i in r.trunk if i in ids]
    return rows


def need_vlans_on(c: Client) -> list[tuple[int, str]]:
    """Refuses when 802.1Q is off; returns the switch's VLANs."""
    html = c.page(VLANS)
    if parse.vlans_enabled(html) is False:
        raise PluginError(
            "802.1Q VLANs are off on the switch (VLAN Manage): turn them on there first"
        )
    return parse.parse_vlans(html)


def set_port_vlans(cfg: Config, change: Change) -> None:
    """Puts a port in VLANs through 802.1Q VLAN Setting: access (one VLAN,
    untagged) or trunk (a native VLAN and tagged ones; these switches always
    have a native VLAN on a trunk). The port's accepted frames go back to
    "All", the switch's default."""
    want = change.value if isinstance(change.value, dict) else None
    n = parse.port_number(change.port or "")
    if want is None or n is None:
        raise PluginError("port.vlans takes a port and {untagged, tagged}")
    untagged = want.get("untagged")
    # The native VLAN is untagged on a trunk: listed as tagged too, it is
    # dropped there, as port_vlans() reads it back.
    tagged = sorted(set(want.get("tagged") or []) - {untagged})
    c = client_from(cfg)
    try:
        vlan_list = need_vlans_on(c)
        html = c.page(PORT_VLANS)
        row = existing_only(parse.parse_port_vlans(html), vlan_list).get(n)
        form = parse.parse_port_forms(html, "page=port_based").get(n)
        if row is None or form is None:
            raise PluginError(f"the switch's VLAN settings have no {change.port}")
        if row.mode == "hybrid":
            raise PluginError(f"{change.port} is a hybrid port: change it on the switch")
        wanted = PortVlans(untagged=untagged, tagged=tagged or None)
        if port_vlans(row) == wanted:
            return
        fields = vlan_form(form, untagged, tagged)
        previous = previous_vlan_form(form, row)

        def shown() -> bool:
            after = existing_only(parse.parse_port_vlans(c.page(PORT_VLANS)), vlan_list).get(n)
            return port_vlans(after) == wanted

        change_and_save(
            c,
            PORT_VLANS,
            fields,
            shown,
            restore(c, PORT_VLANS, previous),
            f"the switch did not apply the VLANs of {change.port}",
        )
    finally:
        c.close()


def previous_vlan_form(form: parse.PortForm, row: parse.PortVlanRow) -> dict[str, str] | None:
    """The fields that put a port's VLANs back as the row shows them, or None
    when the row does not say enough (or names a VLAN the form lacks)."""
    try:
        if row.mode == "access" and row.access:
            return vlan_form(form, row.access, [])
        if row.mode == "trunk" and row.native:
            return vlan_form(form, row.native, [i for i in row.trunk if i != row.native], True)
    except PluginError:
        pass
    return None


def vlan_form(
    form: parse.PortForm, untagged: int | None, tagged: list[int], trunk: bool = False
) -> dict[str, str]:
    """The fields the browser posts for one port's VLANs, in its order: a
    trunk when there are tagged VLANs (or ``trunk``), else access."""

    def option(select: str, vid: int) -> str:
        v = form.options.get(select, {}).get(f"vlan {vid}")
        if v is None:
            raise PluginError(f"the switch has no VLAN {vid}: create it first")
        return v

    if untagged is None:
        raise PluginError("these switches always have a native VLAN on a trunk: choose one")
    fields = {form.checkbox: "on"}
    if not tagged and not trunk:  # access
        fields |= {
            "vlan_type": form.options["vlan_type"]["access"],
            "accept_type": form.options["accept_type"]["all"],
            "vlan_access": option("vlan_access", untagged),
        }
        return fields
    fields |= {
        "vlan_type": form.options["vlan_type"]["trunk"],
        "accept_type": form.options["accept_type"]["all"],
        "vlan_access": option("vlan_access", untagged),
        "vlan_native": option("vlan_native", untagged),
    }
    for vid in sorted({untagged, *tagged}):
        option("vlan_access", vid)  # the switch has it
        fields[f"VLAN{vid}"] = "on"
    return fields


def set_vlan(cfg: Config, change: Change) -> None:
    """Creates a VLAN, or renames it, through Add VLAN (names are cut at
    VLAN_NAME_MAX characters, the switch's limit)."""
    value = change.value if isinstance(change.value, dict) else {}
    vid, name = value.get("id"), (value.get("name") or "")[:VLAN_NAME_MAX]
    if not isinstance(vid, int) or not 1 <= vid <= 4094:
        raise PluginError("vlan.set takes {id, name}")
    c = client_from(cfg)
    try:
        need_vlans_on(c)
        old = dict(parse.parse_vlans(c.page(VLANS))).get(vid)
        if old == name:
            return
        if old is not None:
            # A rename is undone with the old name.
            undo = restore(c, VLANS, {"vid": str(vid), "name": old})
        else:

            def undo() -> None:
                # A new VLAN the switch created anyway cannot be removed
                # (deleting VLANs is not supported yet); one it did not create
                # leaves nothing to undo.
                if vid in dict(parse.parse_vlans(c.page(VLANS))):
                    raise PluginError(f"VLAN {vid} cannot be deleted from Omini")

        change_and_save(
            c,
            VLANS,
            {"vid": str(vid), "name": name},
            lambda: dict(parse.parse_vlans(c.page(VLANS))).get(vid) == name,
            undo,
            f"the switch did not take VLAN {vid} {name!r}",
        )
    finally:
        c.close()


# --- backup ----------------------------------------------------------------------


def backup(cfg: Config) -> list[ConfigBackup]:
    """The switch's settings as sorted JSON, from the pages collect reads: its
    name and address, each port's settings, the 802.1Q VLANs and each port's
    VLANs. No status, counters, uptime or MAC table, and no password (the
    pages never show it), so that two backups differ only when the
    configuration changed. Every page must answer: a partial backup would
    look like a change."""
    c = client_from(cfg)
    try:
        info = parse.parse_info(c.page(INFO))
        if not info:
            raise PluginError("signed in, but this does not look like a supported switch")
        ports = parse.parse_port_config(c.page(PORTS))
        vlan_html = c.page(VLANS)
        port_vlans = parse.parse_port_vlans(c.page(PORT_VLANS))
        settings = {
            "system": {k: info[k] for k in ("name", "ip", "netmask") if info.get(k)},
            "ports": {
                parse.port_name(n): {
                    "enabled": p.enabled,
                    "duplex": p.duplex,
                    "speed": p.speed,
                    "flow_control": p.flow,
                }
                for n, p in ports.items()
            },
            "vlan_manage": parse.vlans_enabled(vlan_html),
            "vlans": [{"id": i, "name": name} for i, name in sorted(parse.parse_vlans(vlan_html))],
            "port_vlans": {
                parse.port_name(n): {
                    "mode": r.mode,
                    "access": r.access,
                    "native": r.native,
                    "trunk": r.trunk,
                }
                for n, r in port_vlans.items()
            },
        }
        return [
            ConfigBackup(
                device=device_key(info, c),
                format="json",
                content=json.dumps(settings, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
            )
        ]
    finally:
        c.close()
