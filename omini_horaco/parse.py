"""Parsers for the switch's web pages.

Tables are found by their headers rather than by position, so small layout
differences between firmware versions (and OEM brands) still parse. Anything
not recognised is left out — never guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

MAC = re.compile(
    r"([0-9a-f]{2})[:-]?([0-9a-f]{2})[:-]?([0-9a-f]{2})[:-]?"
    r"([0-9a-f]{2})[:-]?([0-9a-f]{2})[:-]?([0-9a-f]{2})",
    re.I,
)


def norm_mac(text: str) -> str | None:
    m = MAC.search(text or "")
    return ":".join(g.lower() for g in m.groups()) if m else None


TABLE = re.compile(r"<table\b[^>]*>(.*?)</table>", re.S | re.I)
ROW = re.compile(r"<tr\b[^>]*>(.*?)(?=<tr\b|</table>|$)", re.S | re.I)
# A cell runs until the next cell or the end of the row: the firmware closes
# <th> with </td>, which an HTML parser turns into nested cells.
CELL = re.compile(r"<t[hd]\b[^>]*>(.*?)(?=<t[hd]\b|</tr>|$)", re.S | re.I)
INPUT_VALUE = re.compile(r"<input\b[^>]*\bvalue=[\"']([^\"']*)[\"']", re.I)


def cell_text(raw: str) -> str:
    text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    if not text:
        # An editable value (e.g. the device name) lives in an input box.
        m = INPUT_VALUE.search(raw)
        if m and 'type="text"' in raw.lower().replace("'", '"'):
            text = m.group(1)
    return " ".join(text.split())


def tables(html: str) -> list[list[list[str]]]:
    out = []
    for body in TABLE.findall(html or ""):
        rows = [[cell_text(c) for c in CELL.findall(r)] for r in ROW.findall(body)]
        out.append([r for r in rows if r])
    return out


def port_number(text: str) -> int | None:
    m = re.search(r"(\d+)", text or "")
    return int(m.group(1)) if m else None


def port_name(n: int) -> str:
    return f"Port {n}"


# --- /info.cgi ------------------------------------------------------------------

INFO_KEYS = {
    "device model": "model",
    "model": "model",
    "mac address": "mac",
    "ip address": "ip",
    "netmask": "netmask",
    "subnet mask": "netmask",
    "firmware version": "firmware",
    "firmware": "firmware",
    "hardware version": "hardware",
    "sys uptime": "uptime",
    "system uptime": "uptime",
    "uptime": "uptime",
    "device name": "name",
    "system name": "name",
}


def parse_info(html: str) -> dict[str, str]:
    """System information: a key/value grid (label cell, value cell)."""
    out: dict[str, str] = {}
    for table in tables(html):
        for r in table:
            for i in range(0, len(r) - 1, 2):
                key = INFO_KEYS.get(r[i].strip(" :\uff1a").lower())  # also a fullwidth colon
                if key and r[i + 1] and key not in out:
                    out[key] = r[i + 1]
    return out


def uptime_seconds(text: str | None) -> int | None:
    """'3Day14Hour22Minute8Second' (also with spaces or plurals) → seconds."""
    if not text:
        return None
    units = {"day": 86400, "hour": 3600, "minute": 60, "min": 60, "second": 1, "sec": 1}
    total, found = 0, False
    for value, unit in re.findall(r"(\d+)\s*([a-z]+)", text.lower()):
        for name, secs in units.items():
            if unit.startswith(name):
                total += int(value) * secs
                found = True
                break
    if found:
        return total
    m = re.search(r"(\d+):(\d{2}):(\d{2})", text)
    if m:
        h, mi, s = (int(x) for x in m.groups())
        return h * 3600 + mi * 60 + s
    return None


# --- ports ------------------------------------------------------------------------


@dataclass
class Port:
    number: int
    up: bool | None = None
    speed_mbps: int | None = None
    duplex: str | None = None
    rx_bytes: int | None = None
    tx_bytes: int | None = None
    rx_packets: int | None = None
    tx_packets: int | None = None
    rx_errors: int | None = None
    tx_errors: int | None = None
    media: str | None = None  # "rj45" or "sfp", from the front panel
    extra: dict[str, str] = field(default_factory=dict)


def speed_mbps(text: str) -> int | None:
    """'1000M', '2.5G', '10G', '2500', '1000Full', '10GFull' → Mb/s."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(G|M)?", text or "", re.I)
    if not m:
        return None
    value = float(m.group(1))
    unit = (m.group(2) or "M").upper()
    mbps = round(value * 1000) if unit == "G" else int(value)
    return mbps or None


def duplex(text: str) -> str | None:
    low = (text or "").lower()
    if "full" in low:
        return "full"
    if "half" in low:
        return "half"
    return None


def link_up(text: str) -> bool | None:
    low = (text or "").lower()
    if "down" in low or "off" in low:
        return False
    if "up" in low or low.startswith("link") or low == "on":
        return True
    return None


def header_index(header: list[str], *words: str) -> int | None:
    for i, h in enumerate(header):
        low = h.lower()
        if any(w in low for w in words):
            return i
    return None


def parse_port_status(html: str) -> dict[int, Port]:
    """The port status table: Port | Link | Duplex | Speed (columns found by
    name; 'Speed/Duplex' and 'Link Status' variants included)."""
    ports: dict[int, Port] = {}
    for table in tables(html):
        if len(table) < 2:
            continue
        header = table[0]
        i_port = header_index(header, "port")
        i_link = header_index(header, "link", "status", "state")
        i_speed = header_index(header, "speed")
        if i_port is None or i_link is None or i_speed is None:
            continue
        i_duplex = header_index(header, "duplex")
        for r in table[1:]:
            if len(r) <= max(i_port, i_link, i_speed):
                continue
            n = port_number(r[i_port])
            if n is None:
                continue
            up = link_up(r[i_link])
            p = ports.setdefault(n, Port(number=n))
            p.up = up
            if up:
                p.speed_mbps = speed_mbps(r[i_speed])
                d = r[i_duplex] if i_duplex is not None and i_duplex < len(r) else r[i_speed]
                p.duplex = duplex(d)
        if ports:
            break
    return ports


def counter(text: str) -> int | None:
    """Decimal, hex ('0x1f') or split 64-bit ('hi-lo') counters."""
    t = (text or "").strip().replace(",", "")
    if not t:
        return None
    try:
        if t.lower().startswith("0x"):
            return int(t, 16)
        if re.fullmatch(r"\d+-\d+", t):
            hi, lo = t.split("-")
            return int(hi) * 4_294_967_296 + int(lo)
        return int(t)
    except ValueError:
        return None


def stats_columns(header: list[str]) -> dict[str, int]:
    """Counter columns by name. Bytes only when the header says bytes/octets:
    many models count packets alone, and packets are not bytes."""
    found: dict[str, int] = {}
    for i, raw in enumerate(header):
        h = raw.lower().replace(" ", "")
        side = "tx" if "tx" in h else "rx" if "rx" in h else None
        if not side:
            continue
        if "byte" in h or "octet" in h:
            found.setdefault(f"{side}_bytes", i)
        elif any(w in h for w in ("bad", "err", "drop", "discard")):
            found.setdefault(f"{side}_errors", i)
        elif "pkt" in h or "packet" in h or h in ("tx", "rx") or "good" in h:
            found.setdefault(f"{side}_packets", i)
    return found


def parse_stats(html: str, ports: dict[int, Port]) -> None:
    for table in tables(html):
        if len(table) < 2:
            continue
        cols = stats_columns(table[0])
        if not cols:
            continue
        for r in table[1:]:
            n = port_number(r[0]) if r else None
            if n is None:
                continue
            p = ports.setdefault(n, Port(number=n))
            for name, i in cols.items():
                if i < len(r):
                    setattr(p, name, counter(r[i]))
        return


PANEL_PORT = re.compile(
    r"""name=["']p(\d+)_[^"']*["'][^>]*>\s*<div\b[^>]*class=["']([^"']+)""", re.I
)


def parse_panel(html: str, count: int) -> dict[int, str]:
    """Copper or fibre per port from the front panel: one element per port
    (``<div class="port" name="p9_l1_s4..."><div class="portfiberlnkup">``),
    or images named RJ45_* / Fiber_* on other firmware, in physical order."""
    found = {
        int(n): ("sfp" if "fiber" in cls.lower() or "sfp" in cls.lower() else "rj45")
        for n, cls in PANEL_PORT.findall(html or "")
    }
    if found:
        return found if len(found) == count else {}
    media = []
    for img in BeautifulSoup(html or "", "html.parser").find_all("img"):
        src = str(img.get("src") or "").rsplit("/", 1)[-1].lower()
        if src.startswith("rj45"):
            media.append("rj45")
        elif src.startswith(("fiber", "fibre", "sfp")):
            media.append("sfp")
    if not media or len(media) != count:
        return {}
    return {i: m for i, m in enumerate(media, start=1)}


# --- MAC table --------------------------------------------------------------------


@dataclass
class FdbRow:
    mac: str
    port: int
    vlan: int | None = None


def parse_mac_table(html: str) -> list[FdbRow]:
    """Learned MAC addresses: a table with MAC and Port columns (VLAN if any)."""
    out: list[FdbRow] = []
    for table in tables(html):
        if len(table) < 2:
            continue
        header = table[0]
        i_mac = header_index(header, "mac")
        i_port = header_index(header, "port")
        if i_mac is None or i_port is None or i_mac == i_port:
            continue
        i_vlan = header_index(header, "vlan", "vid")
        for r in table[1:]:
            if len(r) <= max(i_mac, i_port):
                continue
            mac = norm_mac(r[i_mac])
            port = port_number(r[i_port])
            if not mac or port is None:
                continue
            vlan = port_number(r[i_vlan]) if i_vlan is not None and i_vlan < len(r) else None
            out.append(FdbRow(mac=mac, port=port, vlan=vlan))
    return out


def mac_pages(html: str) -> int:
    """Pages of the MAC table ('1 / <label id='totalpage'>2</label> Pages'), 1 if none."""
    m = re.search(r"id=['\"]?totalpage['\"]?[^>]*>\s*(\d+)", html or "", re.I)
    return max(1, int(m.group(1))) if m else 1


def mac_per_page(html: str) -> str | None:
    """The selected 'items per page' option, sent back when turning pages."""
    soup = BeautifulSoup(html or "", "html.parser")
    sel = soup.find("select", attrs={"name": "perpage"})
    opt = sel and (sel.find("option", selected=True) or sel.find("option"))
    return str(opt.get("value")) if opt else None


# --- /port.cgi: port configuration ------------------------------------------------


@dataclass
class PortConfig:
    """A port's configuration as the Port List shows it (the texts of its
    "Config Attribute" columns)."""

    number: int
    enabled: bool | None
    duplex: str
    speed: str
    flow: str


def parse_port_config(html: str) -> dict[int, PortConfig]:
    """The Port List: Port | State | Duplex (config, actual) | Speed (config,
    actual) | Flow Control (config, actual)."""
    out: dict[int, PortConfig] = {}
    for table in tables(html):
        for r in table:
            n = port_number(r[0]) if r and r[0].lower().startswith("port ") else None
            if n is None or len(r) < 8:
                continue
            state = r[1].lower()
            enabled = (
                True
                if state.startswith("enable")
                else False
                if state.startswith("disable")
                else None
            )
            out[n] = PortConfig(number=n, enabled=enabled, duplex=r[2], speed=r[4], flow=r[6])
        if out:
            break
    return out


@dataclass
class PortForm:
    """The form that configures a port: its checkbox, its hidden fields and,
    per select, the option value of each text ("Disable" → "0")."""

    checkbox: str
    hidden: dict[str, str]
    options: dict[str, dict[str, str]]


def option_text(option) -> str:
    """An option's own text: the firmware leaves <option> unclosed, so the next
    options nest inside it."""
    return " ".join(next(iter(option.strings), "").split()).lower()


def parse_port_forms(html: str, action: str = "port.cgi") -> dict[int, PortForm]:
    """The forms that pick ports (Port Setting: one for the RJ45 ports, one for
    the SFP+ port; 802.1Q VLAN Setting), by port number: a port is picked with
    its checkbox, labelled "Port N"."""
    out: dict[int, PortForm] = {}
    soup = BeautifulSoup(html or "", "html.parser")
    for form in soup.find_all("form"):
        if action not in (form.get("action") or ""):
            continue
        options = {
            s.get("name"): {option_text(o): o.get("value") for o in s.find_all("option")}
            for s in form.find_all("select")
            if s.get("name")
        }
        hidden = {
            i.get("name"): i.get("value", "")
            for i in form.find_all("input", type="hidden")
            if i.get("name")
        }
        for box in form.find_all("input", type="checkbox"):
            label = form.find("label", attrs={"for": box.get("id")})
            text = label.get_text() if label else ""
            n = port_number(text) if "port" in text.lower() else None
            if n is not None and box.get("name"):
                out[n] = PortForm(checkbox=box["name"], hidden=hidden, options=options)
    return out


# --- /vlan.cgi: 802.1Q VLANs ------------------------------------------------------


def vlans_enabled(html: str) -> bool | None:
    """VLAN Manage (the switch-wide 802.1Q switch), when the page says."""
    box = BeautifulSoup(html or "", "html.parser").find("input", attrs={"name": "enable_vlan"})
    return None if box is None else box.has_attr("checked")


def parse_vlans(html: str) -> list[tuple[int, str]]:
    """The VLANs of the switch (Add VLAN's list): (id, name)."""
    out: list[tuple[int, str]] = []
    for table in tables(html):
        if not table:
            continue
        header = table[0]
        i_id, i_name = header_index(header, "vlan id"), header_index(header, "vlan name")
        if i_id is None:
            continue
        for r in table[1:]:
            if len(r) > i_id and r[i_id].isdigit():
                name = r[i_name] if i_name is not None and i_name < len(r) else ""
                out.append((int(r[i_id]), name))
        if out:
            break
    return out


@dataclass
class PortVlanRow:
    """A port's row of the 802.1Q VLAN Setting list."""

    number: int
    mode: str  # "access", "trunk" or "hybrid"
    access: int | None
    native: int | None
    trunk: list[int]


def vlan_ids(text: str) -> list[int]:
    """ "1,10,20" or "1-4094" (with spaces or zero-width spaces) → ids."""
    out: list[int] = []
    for part in re.split(r"[,\s​]+", text or ""):
        if m := re.fullmatch(r"(\d+)-(\d+)", part):
            out.extend(range(int(m.group(1)), int(m.group(2)) + 1))
        elif part.isdigit():
            out.append(int(part))
    return sorted(set(out))


def parse_port_vlans(html: str) -> dict[int, PortVlanRow]:
    """802.1Q VLAN Setting's list: Port | Port vlan type | Access VLAN |
    Native VLAN | Trunk vlan."""
    out: dict[int, PortVlanRow] = {}
    for table in tables(html):
        if not table or header_index(table[0], "vlan type") is None:
            continue
        for r in table[1:]:
            n = port_number(r[0]) if r and r[0].lower().startswith("port ") else None
            if n is None or len(r) < 5:
                continue
            mode = r[1].strip().lower()
            num = lambda s: int(s) if s.strip().isdigit() else None  # noqa: E731
            out[n] = PortVlanRow(
                number=n, mode=mode, access=num(r[2]), native=num(r[3]), trunk=vlan_ids(r[4])
            )
        if out:
            break
    return out
