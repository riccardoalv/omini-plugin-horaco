"""Turns the switch's web pages into an Omini device."""

from __future__ import annotations

from omini_sdk import Config, Device, FdbEntry, Interface, PluginError, log

from omini_horaco import parse
from omini_horaco.client import Client

INFO = "/info.cgi"
STATS = "/port.cgi?page=stats"
PANEL = "/panel.cgi"
MACS = "/mac.cgi?page=fwd_tbl"
# The MAC table is paged on this firmware; never read more than this many pages.
MAX_MAC_PAGES = 64


def client_from(cfg: Config) -> Client:
    host, user, password = cfg.str("host"), cfg.str("username", "admin"), cfg.str("password")
    if not host or not user or not password:
        raise PluginError("the address, username and password are required")
    pages = cfg.state_dir / "pages" if cfg.state_dir else None
    return Client(host, user, password, pages_dir=pages)


def optional(what: str, fn, default):
    """Optional pages: one failing never fails the collection."""
    try:
        return fn()
    except PluginError:
        raise
    except Exception:
        log.exception("could not read %s", what)
        return default


def mac_table(c: Client) -> list[parse.FdbRow]:
    """All pages of the MAC table. The firmware turns pages with its form
    (cmd=goto), which changes nothing on the switch: it only picks the page."""
    first = c.page(MACS)
    out = parse.parse_mac_table(first)
    pages = min(parse.mac_pages(first), MAX_MAC_PAGES)
    per_page = parse.mac_per_page(first)
    for i in range(2, pages + 1):
        form = {"cmd": "goto", "pageidx": str(i)}
        if per_page:
            form["perpage"] = per_page
        out += parse.parse_mac_table(c.page(MACS, form=form))
    # Pages may overlap while the table changes: one entry per MAC and port.
    seen: set[tuple[str, int]] = set()
    unique = []
    for r in out:
        if (r.mac, r.port) not in seen:
            seen.add((r.mac, r.port))
            unique.append(r)
    return unique


def interfaces(ports: dict[int, parse.Port]) -> list[Interface]:
    out = []
    for n in sorted(ports):
        p = ports[n]
        out.append(
            Interface(
                name=parse.port_name(n),
                type="ethernet",
                connector=p.media,
                up=p.up,
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
        if not ports:
            # Some models keep the status table on the port page.
            ports = optional("ports", lambda: parse.parse_port_status(c.page("/port.cgi")), {})
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
                interfaces=interfaces(ports) or None,
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
