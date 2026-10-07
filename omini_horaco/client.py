"""HTTP client for the switch's web interface.

The firmware's embedded web server is fragile: it drops connections when
requests arrive back to back and sometimes closes one without an answer. So
requests go one at a time, with a short pause between them, and are retried.

Signing in mirrors the login page: the browser sends MD5(username + password)
as the form's ``Response`` field and keeps it in the ``admin`` cookie, which is
what authenticates every later page. Only pages are read: nothing is posted
besides the login form and page navigation of the MAC table.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import httpx
from omini_sdk import PluginError, log

# Pause between requests and attempts per page (see the module docstring).
DELAY_S = 0.4
ATTEMPTS = 4


def credential(username: str, password: str) -> str:
    return hashlib.md5((username + password).encode()).hexdigest()


def signed_out(html: str) -> bool:
    """The firmware answers a page without a valid session with a redirect
    script to the login page (HTTP 200), or with the login form itself."""
    low = html.lower()
    return (
        'location.replace("/login.cgi")' in low
        or 'name ="login"' in low
        or ('name="login"' in low and "inpwd" in low)
    )


class Client:
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        timeout: float = 10,
        pages_dir: Path | None = None,
        transport: httpx.BaseTransport | None = None,
        delay_s: float = DELAY_S,
    ):
        host = host.strip().rstrip("/")
        if not host.startswith(("http://", "https://")):
            host = "http://" + host
        self.base = host
        self.username = username
        self.password = password
        self.delay_s = delay_s
        # The last answer of each page, kept to help adapt the parsers to
        # other firmware versions (local only, in the plugin's state folder).
        self.pages_dir = pages_dir
        self.http = httpx.Client(
            base_url=host,
            timeout=timeout,
            headers={"User-Agent": "omini-plugin-horaco", "Referer": host + "/"},
            transport=transport,
        )
        self.logged_in = False

    def close(self) -> None:
        self.http.close()

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(ATTEMPTS):
            if self.delay_s:
                # Longer pauses after a dropped connection: the server needs time.
                time.sleep(self.delay_s * (1 + 3 * attempt))
            try:
                return self.http.request(method, path, **kw)
            except httpx.ConnectError as e:
                raise PluginError(f"cannot connect to {self.base}: {e}") from e
            except httpx.HTTPError as e:
                last = e
        raise PluginError(f"{self.base} did not answer {path}: {last}")

    def login(self) -> None:
        token = credential(self.username, self.password)
        self.http.cookies.set("admin", token)
        r = self._request(
            "POST",
            "/login.cgi",
            data={
                "username": self.username,
                "password": self.password,
                "Response": token,
                "language": "EN",
            },
            headers={"Referer": self.base + "/login.cgi"},
        )
        if r.status_code >= 400:
            raise PluginError(f"the switch answered {r.status_code} to the login")
        # A wrong password brings the login form back with an error message.
        if "inpwd" in r.text and ("error" in r.text.lower() or "错误" in r.text):
            raise PluginError("the switch rejected the username or password")
        self.logged_in = True

    def page(self, path: str, form: dict[str, str] | None = None) -> str:
        """Reads a page, signing in first (and again when the session expired).
        ``form`` turns a page of a paged table (only ``cmd=goto`` is allowed:
        other commands of those forms change the switch, e.g. clear a table)."""
        if form is not None and form.get("cmd") != "goto":
            raise ValueError("only page navigation may be posted")
        if not self.logged_in:
            self.login()
        for again in (True, False):
            if form is None:
                r = self._request("GET", path)
            else:
                r = self._request("POST", path, data=form, headers={"Referer": self.base + path})
            if r.status_code == 404:
                return ""
            if r.status_code >= 400:
                raise PluginError(f"the switch answered {r.status_code} for {path}")
            html = r.text
            if not signed_out(html):
                self._keep(path + (f"&pageidx={form['pageidx']}" if form else ""), html)
                return html
            if again:
                log.info("session expired, signing in again")
                self.login()
        raise PluginError("the switch rejected the username or password")

    def _keep(self, path: str, html: str) -> None:
        if not self.pages_dir:
            return
        try:
            self.pages_dir.mkdir(parents=True, exist_ok=True)
            name = path.strip("/").replace("?", "_").replace("=", "-").replace("&", "_")
            (self.pages_dir / f"{name}.html").write_text(html)
        except OSError:
            log.debug("could not keep a copy of %s", path)
