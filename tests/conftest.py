from functools import partial
from pathlib import Path

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

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(f"{request.method} {page_name(request.url)}")
        form = {}
        if request.method == "POST":
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            self.posts.append(f"{request.url.path} {form.get('cmd', '')}".strip())
            if request.url.path == "/login.cgi":
                if form.get("Response") != credential(self.user, self.password):
                    return httpx.Response(200, text=LOGIN_FAILED)
                return httpx.Response(200, text="<html>ok</html>")
        cookie = request.headers.get("cookie", "")
        if f"admin={credential(self.user, self.password)}" not in cookie or self.expire_once:
            self.expire_once = False
            return httpx.Response(200, text=REDIRECT)
        name = page_name(request.url)
        if form.get("cmd") == "goto":
            name += f"_pageidx-{form['pageidx']}"
        body = self.pages.get(name)
        if body is None:
            return httpx.Response(404, text="Not Found")
        return httpx.Response(200, text=body)


@pytest.fixture
def switch(monkeypatch):
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
