"""Parsers for the switch's web pages.

Tables are found by their headers rather than by position, so small layout
differences between firmware versions (and OEM brands) still parse. Anything
not recognised is left out — never guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Tag

MAC = re.compile(
    r"([0-9a-f]{2})[:-]?([0-9a-f]{2})[:-]?([0-9a-f]{2})[:-]?"
    r"([0-9a-f]{2})[:-]?([0-9a-f]{2})[:-]?([0-9a-f]{2})",
    re.I,
)


def norm_mac(text: str) -> str | None:
    m = MAC.search(text or "")
    return ":".join(g.lower() for g in m.groups()) if m else None


def cell_text(c: Tag) -> str:
    return " ".join(c.get_text(" ", strip=True).split())


def rows(table: Tag) -> list[list[str]]:
    return [[cell_text(c) for c in tr.find_all(["td", "th"])] for tr in table.find_all("tr")]


def tables(html: str) -> list[list[list[str]]]:
    soup = BeautifulSoup(html or "", "html.parser")
    # Innermost tables only: some pages nest a layout table around the data.
    return [rows(t) for t in soup.find_all("table") if not t.find("table")]


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


def parse_panel(html: str, count: int) -> dict[int, str]:
    """Copper or fibre per port from the front-panel images (RJ45_*, Fiber_*),
    in physical order. Nothing when the count does not match the ports."""
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
    """How many pages the MAC table has (a page selector or 'n/N'), 1 if none."""
    soup = BeautifulSoup(html or "", "html.parser")
    sel = soup.find("select", attrs={"name": re.compile("page", re.I)})
    if sel:
        return max(1, len(sel.find_all("option")))
    m = re.search(r"\b\d+\s*/\s*(\d+)\b", soup.get_text(" "))
    return int(m.group(1)) if m else 1
