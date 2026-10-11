"""Turns the switch's web pages into an Omini device."""

from __future__ import annotations

import time

from omini_sdk import Change, Config, Device, FdbEntry, Interface, PluginError, log

from omini_horaco import parse
from omini_horaco.client import Client, Rejected, Unanswered

INFO = "/info.cgi"
STATS = "/port.cgi?page=stats"
PANEL = "/panel.cgi"
MACS = "/mac.cgi?page=fwd_tbl"
PORTS = "/port.cgi"
SAVE = "/save.cgi"
# The MAC table is paged on this firmware; never read more than this many pages.
MAX_MAC_PAGES = 64


def client_from(cfg: Config) -> Client:
    host, user, password = cfg.str("host"), cfg.str("username", "admin"), cfg.str("password")
    if not host or not user or not password:
        raise PluginError("the address, username and password are required")
    pages = cfg.state_dir / "pages" if cfg.state_dir else None
    return Client(host, user, password, pages_dir=pages)


def optional(what: str, fn, default):
    """Optional pages: one failing never fails the collection (a wrong
    password does)."""
    try:
        return fn()
    except Rejected:
        raise
    except Unanswered as e:
        log.warning("skipped %s: %s", what, e)
        return default
    except Exception:
        log.exception("could not read %s", what)
        return default


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
    ports: dict[int, parse.Port], config: dict[int, parse.PortConfig]
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
                speed_mbps=p.speed_mbps,
                duplex=p.duplex,
                rx_bytes=p.rx_bytes,
                tx_bytes=p.tx_bytes,
                rx_errors=p.rx_errors,
                tx_errors=p.tx_errors,
            )
        )
    return out


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

        mac = parse.norm_mac(info.get("mac", ""))
        host = c.base.split("://", 1)[-1]
        model = info.get("model")
        return [
            Device(
                key=mac or host,
                name=info.get("name") or model or host,
                host=host,
                role="switch",
                vendor="Horaco" if model and model.upper().startswith("HC-") else None,
                model=model,
                os_version=info.get("firmware"),
                uptime_s=parse.uptime_seconds(info.get("uptime")),
                macs=[mac] if mac else None,
                ips=[info["ip"]] if info.get("ip") else [host],
                interfaces=interfaces(ports, config) or None,
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
    """Enables or disables a port (port.enabled): posts the port's form with
    its other settings as they are, reads the page again to check the switch
    took it, and only then saves the configuration (unsaved, a reboot undoes
    it). Returns the switch as read after the change."""
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
            c.submit(PORTS, port_form(form, current, change.value))
            after = parse.parse_port_config(c.page(PORTS)).get(n)
            if after is None or after.enabled != change.value:
                raise PluginError(
                    f"the switch did not apply the change to {change.port} (not saved)"
                )
            c.submit(SAVE, {"cmd": "save"})
    finally:
        c.close()
    return collect(cfg)


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
