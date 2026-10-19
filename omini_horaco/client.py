"""HTTP client for the switch's web interface, on the SDK's HTTP client.

The firmware's embedded web server is fragile: it drops connections when
requests arrive back to back and sometimes closes one without an answer. So
requests go one at a time, with a short pause between them; the SDK's client
asks again after a refused connection (any request), a dropped answer or a
timeout (reads only: a form posted is never sent twice).

Signing in mirrors the login page: the browser sends MD5(username + password)
as the form's ``Response`` field and keeps it in the ``admin`` cookie, which is
what authenticates every later page. Collecting only reads pages: nothing is
posted besides the login form and page navigation of the MAC table; a change
(``submit``) posts the form of its page, then the page is read again.

The switch speaks plain HTTP only (no TLS): an address without a scheme gets
``http://``.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from pathlib import Path

import httpx
from omini_sdk import log
from omini_sdk.http import Client as HttpClient
from omini_sdk.http import Unauthorized, Unavailable

# Pause between requests, and how many times a request is asked again (see
# the module docstring); the pauses between tries grow from RETRY_PAUSE_S.
DELAY_S = 0.4
RETRIES = 3
RETRY_PAUSE_S = 1.2


class Rejected(Unauthorized):
    """Wrong username or password: never retried, never hidden."""


# The switch did not answer (refused or dropped every try): the SDK's error.
Unanswered = Unavailable


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


class Client(HttpClient):
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        timeout: float = 15,
        pages_dir: Path | None = None,
        transport: httpx.BaseTransport | None = None,
        delay_s: float = DELAY_S,
        sleep: Callable[[float], None] | None = None,
    ):
        super().__init__(
            host,
            name="the switch",
            scheme="http",
            timeout=timeout,
            retries=RETRIES,
            backoff=RETRY_PAUSE_S,
            user_agent="omini-plugin-horaco",
            # One request per connection: the embedded server mishandles kept-alive ones.
            headers={"Accept": "text/html,*/*", "Connection": "close"},
            unauthorized="the switch rejected the username or password",
            transport=transport,
            sleep=sleep,
        )
        self.http.headers["Referer"] = self.base + "/"
        self.username = username
        self.password = password
        self.delay_s = delay_s
        # The last answer of each page, kept to help adapt the parsers to
        # other firmware versions (local only, in the plugin's state folder).
        self.pages_dir = pages_dir
        # The session cookie is MD5(username + password), the same on every
        # sign-in: pages are read with it directly, and the login form is only
        # posted when the switch asks for it (it keeps a single session and
        # drops connections when signed in again and again).
        self.http.cookies.set("admin", credential(username, password))
        self.logged_in = True

    def send(self, method: str, path: str, **kw) -> httpx.Response:
        """One request, after a short pause (the server needs it), with the
        SDK's retries."""
        if self.delay_s:
            (self.sleep or time.sleep)(self.delay_s)
        return super().send(method, path, **kw)

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        return self.send(method, path, **kw)

    def login(self) -> None:
        token = credential(self.username, self.password)
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
        self.check(r, "POST", "/login.cgi")
        # A wrong password brings the login form back with an error message.
        if "inpwd" in r.text and ("error" in r.text.lower() or "错误" in r.text):
            raise Rejected("the switch rejected the username or password")
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
                # The switch drops a form posted right after a page: wait more.
                time.sleep(self.delay_s * 2)
                r = self._request("POST", path, data=form, headers={"Referer": self.base + path})
            if r.status_code == 404:
                return ""
            self.check(r, "POST" if form else "GET", path)
            html = r.text
            if not signed_out(html):
                self._keep(path + (f"&pageidx={form['pageidx']}" if form else ""), html)
                return html
            if again:
                log.info("session expired, signing in again")
                self.login()
        raise Rejected("the switch rejected the username or password")

    def submit(self, path: str, form: dict[str, str]) -> None:
        """Posts a configuration form (a change): only ``apply`` calls it, for
        the changes the manifest declares. Whether it took effect is checked
        by reading the page again, never assumed from the answer."""
        if not self.logged_in:
            self.login()
        for again in (True, False):
            time.sleep(self.delay_s * 2)  # the switch drops a form posted right after a page
            r = self._request("POST", path, data=form, headers={"Referer": self.base + path})
            self.check(r, "POST", path)
            if not signed_out(r.text):
                return
            if again:
                log.info("session expired, signing in again")
                self.login()
        raise Rejected("the switch rejected the username or password")

    def _keep(self, path: str, html: str) -> None:
        if not self.pages_dir:
            return
        try:
            self.pages_dir.mkdir(parents=True, exist_ok=True)
            name = path.strip("/").replace("?", "_").replace("=", "-").replace("&", "_")
            (self.pages_dir / f"{name}.html").write_text(html)
        except OSError:
            log.debug("could not keep a copy of %s", path)
