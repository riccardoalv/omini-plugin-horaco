import re
from functools import partial
from pathlib import Path
from urllib.parse import unquote_plus

import httpx
import pytest

import omini_horaco.collect as collect_module
from omini_horaco.client import Client, credential

FIXTURES = Path(__file__).parent / "fixtures"

REDIRECT = '<script type="text/javascript">window.top.location.replace("/login.cgi");</script>'
LOGIN_FAILED = (
    '<form method="post" name ="login" action="login.cgi">'
    '<input type="password" id="inpwd" name="password">'
    '<label id="tip">Username、Password error</label></form>'
)


def page_name(url: httpx.URL) -> str:
    """The same file names the client uses to keep pages."""
    path = url.path.strip("/") + (f"?{url.query.decode()}" if url.query else "")
    return path.replace("?", "_").replace("=", "-").replace("&", "_")


class FakeSwitch:
    """Answers like the switch's web interface, from the HTML files in fixtures/."""

    def __init__(self):
        self.pages = {f.stem: f.read_text() for f in FIXTURES.glob("*.html")}
        self.user, self.password = "admin", "secret"
        self.calls = []
        self.posts = []
        self.expire_once = False
        # The switch remembers the last session: the cookie works until it
        # reboots or someone signs in with other credentials.
        self.session = True
        self.drop_paging = False  # the switch drops the page navigation form
        self.drop_paging_times = 0  # ... only this many times, then answers
        # Port configuration: what Port Setting posted, and the Port List state.
        self.forms: list[dict[str, str]] = []
        self.disabled: set[int] = set()
        self.ignore_changes = False  # answers the form but changes nothing
        self.saved = 0
        # 802.1Q: rows of 802.1Q VLAN Setting posted, by port; VLANs added or renamed.
        self.port_vlans: dict[int, str] = {}
        self.vlan_names: dict[int, str] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(f"{request.method} {page_name(request.url)}")
        form = {}
        if request.method == "POST":
            pairs = (x.split("=", 1) for x in request.content.decode().split("&"))
            form = {k: unquote_plus(v) for k, v in pairs}
            self.posts.append(f"{request.url.path} {form.get('cmd', '')}".strip())
            if form.get("cmd") == "goto" and (self.drop_paging or self.drop_paging_times > 0):
                self.drop_paging_times -= 1
                raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
            if request.url.path == "/login.cgi":
                if form.get("Response") != credential(self.user, self.password):
                    return httpx.Response(200, text=LOGIN_FAILED)
                self.session = True
                return httpx.Response(200, text="<html>ok</html>")
        cookie = request.headers.get("cookie", "")
        valid = f"admin={credential(self.user, self.password)}" in cookie
        if not valid or not self.session or self.expire_once:
            self.expire_once = False
            self.session = self.session and valid
            return httpx.Response(200, text=REDIRECT)
        name = page_name(request.url)
        if form.get("cmd") == "goto":
            name += f"_pageidx-{form['pageidx']}"
        if request.url.path == "/port.cgi" and form.get("cmd") == "port":
            self.forms.append(form)
            if not self.ignore_changes:
                for key in form:
                    if re.fullmatch(r"port\d+", key):
                        n = int(key[4:]) + 1
                        (self.disabled.add if form["state"] == "0" else self.disabled.discard)(n)
        page = request.url.params.get("page")
        if request.url.path == "/vlan.cgi" and page == "port_based" and "vlan_type" in form:
            self.forms.append(form)
            if not self.ignore_changes:
                for key in form:
                    if re.fullmatch(r"port\d+", key):
                        self.port_vlans[int(key[4:]) + 1] = vlan_row(form)
        if request.url.path == "/vlan.cgi" and page == "static" and "vid" in form:
            self.forms.append(form)
            if not self.ignore_changes:
                self.vlan_names[int(form["vid"])] = form.get("name", "")
        if request.url.path == "/save.cgi" and form.get("cmd") == "save":
            self.saved += 1
        body = self.pages.get(name)
        if body is None:
            return httpx.Response(404, text="Not Found")
        if name == "vlan.cgi_page-port_based":
            for n, cells in self.port_vlans.items():
                body = re.sub(
                    rf"<tr><td>Port {n}</td>.*?</tr>", f"<tr><td>Port {n}</td>{cells}</tr>", body
                )
        if name == "vlan.cgi_page-static":
            for vid, vname in self.vlan_names.items():
                cells = r"<tr><td>[^<]*(?:<input[^>]*>)?</td><td>\d+</td>"
                row = rf"({cells}<td>{vid}</td><td>)[^<]*(</td></tr>)"
                if re.search(row, body):
                    body = re.sub(row, rf"\g<1>{vname}\g<2>", body)
                else:
                    new = f"<tr><td></td><td>0</td><td>{vid}</td><td>{vname}</td></tr>"
                    body = body.replace(
                        '</table><input type="button" value="Select All"',
                        new + '</table><input type="button" value="Select All"',
                    )
        if name == "port.cgi":
            for n in self.disabled:
                row = f"<td>Port {n}</td>"
                body = body.replace(row + "<td>Enable</td>", row + "<td>Disable</td>")
        return httpx.Response(200, text=body)


@pytest.fixture
def switch(monkeypatch):
    monkeypatch.setattr(collect_module, "BUSY_PAUSE_S", 0)
    monkeypatch.setattr("time.sleep", lambda _s: None)  # no pauses between tries
    fake = FakeSwitch()
    monkeypatch.setattr(
        collect_module,
        "Client",
        partial(Client, transport=httpx.MockTransport(fake.handler), delay_s=0),
    )
    return fake


@pytest.fixture
def cfg(tmp_path):
    from omini_sdk import Config

    return Config({"host": "192.168.1.96", "username": "admin", "password": "secret"}, tmp_path)


def vlan_row(form: dict[str, str]) -> str:
    """The list cells 802.1Q VLAN Setting shows after a post."""
    if form["vlan_type"] == "0":
        return f"<td>Access</td><td>{form['vlan_access']}</td><td>-</td><td>-</td>"
    allowed = sorted(int(k[4:]) for k in form if re.fullmatch(r"VLAN\d+", k))
    trunk = ",".join(str(v) for v in allowed)
    access, native = form["vlan_access"], form["vlan_native"]
    return f"<td>Trunk</td><td>{access}</td><td>{native}</td><td>{trunk}</td>"
