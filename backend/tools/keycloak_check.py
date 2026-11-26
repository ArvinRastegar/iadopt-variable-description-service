"""Try the sign-in against the local Keycloak of ``deploy/keycloak`` (keycloak-deployment, section 7).

Run by hand, outside the tests and CI, beside the application's own stack with that Keycloak as its
provider (``deploy/keycloak/README.md``, "The local stack")::

    KEYCLOAK_ADMIN_USERNAME=... KEYCLOAK_ADMIN_PASSWORD=... .venv/bin/python backend/tools/keycloak_check.py

It plays the browser and prints one line per check: what ``oidc check`` says of the provider; that somebody who
registers and confirms the address signs in; that an address which is not verified is refused; that signing out
ends both sessions; and that the provider can end one by itself. It leaves ordinary accounts in the application's
database, with made-up addresses, and nothing in Keycloak, which forgets everything when it is stopped.

It sends nothing to an address that is not this machine.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import pathlib
import re
import secrets
import socket
import sys
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urljoin, urlsplit

import requests

BACKEND = pathlib.Path(__file__).resolve().parents[1]
SECONDS = 15.0
"""How long one request, and what follows from one, may take."""


def is_this_machine(url: str) -> bool:
    """Return whether an address is this machine: ``localhost``, a name ending in ``.localhost``, or a loopback address.

    Args:
        url: The address.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or "@" in parts.netloc or "\\" in url or port == 0:
        return False
    return host in ("localhost", "127.0.0.1", "::1") or (host.endswith(".localhost") and host.isascii())


def stay_on_this_machine() -> None:
    """Find no other machine, whatever asks: also the requests that ``oidc check`` sends where the provider says.

    Every ``.localhost`` name is the loopback address, whatever a name server says; ``localhost`` and loopback
    addresses are themselves; any other name or address is not found. No proxy is used.
    """
    original = socket.getaddrinfo

    def resolve(host: Any, *args: Any, **kwargs: Any) -> Any:
        name = host.decode("ascii", "replace") if isinstance(host, bytes) else host
        if isinstance(name, str) and name.lower().endswith(".localhost"):
            return original("127.0.0.1", *args, **kwargs)
        if name is None or _is_loopback(name):
            return original(host, *args, **kwargs)
        raise socket.gaierror(socket.EAI_NONAME, f"{name} is not this machine")

    socket.getaddrinfo = resolve
    os.environ.update(NO_PROXY="*", no_proxy="*")


def _is_loopback(name: str) -> bool:
    """Return whether a name is ``localhost`` or a loopback address.

    Args:
        name: The name, as a resolver gets it.
    """
    if name.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


# ------------------------------------------------------------------------------------------------
# A browser
# ------------------------------------------------------------------------------------------------


class Failed(Exception):
    """A step that did not do what it should."""


@dataclass
class Form:
    """A form of a page: where it goes, and its fields with the values it came with."""

    action: str
    fields: Dict[str, str] = field(default_factory=dict)


class Page(HTMLParser):
    """The forms and links of a page."""

    def __init__(self, response: requests.Response) -> None:
        """Read a page.

        Args:
            response: The answer that holds it.
        """
        super().__init__()
        self.url, self.text = response.url, response.text
        self.forms: List[Form] = []
        self.links: List[str] = []
        self._open: Optional[Form] = None
        self.feed(response.text)

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        """Keep forms, their fields and links.

        Args:
            tag: The element's name.
            attrs: Its attributes.
        """
        values = {name: value or "" for name, value in attrs}
        if tag == "form":
            self._open = Form(urljoin(self.url, values.get("action", "")))
            self.forms.append(self._open)
        elif tag in ("input", "button") and self._open is not None and values.get("name"):
            self._open.fields[values["name"]] = values.get("value", "")
        elif tag == "a" and values.get("href"):
            self.links.append(urljoin(self.url, values["href"]))

    def handle_endtag(self, tag: str) -> None:
        """Close a form.

        Args:
            tag: The element's name.
        """
        if tag == "form":
            self._open = None

    def form_with(self, *names: str) -> Form:
        """Return the form that has all these fields.

        Args:
            names: The fields' names.

        Raises:
            Failed: When there is none.
        """
        for form in self.forms:
            if all(name in form.fields for name in names):
                return form
        raise Failed(f"the page has no form with {', '.join(names)}: {self.heading()}")

    def heading(self) -> str:
        """Return the page's words in short, for a message."""
        return re.sub(r"\s+", " ", re.sub(r"<(script|style).*?</\1>|<[^>]+>", " ", self.text, flags=re.DOTALL)).strip()[:160]


class Browser:
    """One browser: its cookies, and pages of this machine only."""

    def __init__(self) -> None:
        """Start without a cookie."""
        self.session = requests.Session()
        self.session.trust_env = False

    def send(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Send one request and follow no redirect.

        Args:
            method: The HTTP method.
            url: The address.
            **kwargs: For ``requests``.

        Raises:
            Failed: When the address is not this machine.
        """
        if not is_this_machine(url):
            raise Failed(f"the trip leads to another machine: {urlsplit(url).netloc}")
        answer = self.session.request(method, url, allow_redirects=False, timeout=SECONDS, **kwargs)
        for cookie in self.session.cookies:
            cookie.secure = False  # like a browser, which sends Secure cookies to this machine over http too
        return answer

    def follow(self, response: requests.Response, until: str) -> requests.Response:
        """Follow redirects to a page, or to the first one that leads to an address beginning with ``until``.

        Args:
            response: The answer to begin with.
            until: The address to stop at; the answer that leads there is returned.

        Raises:
            Failed: When the trip does not end.
        """
        for _ in range(12):
            location = response.headers.get("Location")
            if response.status_code not in (301, 302, 303, 307, 308) or not location:
                return response
            target = urljoin(response.url, location)
            if target.startswith(until):
                return response
            response = self.send("GET", target)
        raise Failed("the trip does not end")

    def submit(self, form: Form, typed: Optional[Dict[str, str]] = None) -> requests.Response:
        """Send a form.

        Args:
            form: The form.
            typed: What is typed into its fields.
        """
        return self.send("POST", form.action, data={**form.fields, **(typed or {})})


# ------------------------------------------------------------------------------------------------
# The checks
# ------------------------------------------------------------------------------------------------


@dataclass
class Places:
    """Where everything is, and the administrator of the local Keycloak."""

    issuer: str
    client_id: str
    public_url: str
    mail: str
    admin: str
    password: str

    @property
    def keycloak(self) -> str:
        """Keycloak's own address, before the realm."""
        return self.issuer.partition("/realms/")[0]

    @property
    def realm(self) -> str:
        """The realm's administration."""
        return f"{self.keycloak}/admin/realms/{self.issuer.rpartition('/')[2]}"


@dataclass
class Person:
    """Somebody with a made-up address."""

    email: str = field(default_factory=lambda: f"person-{secrets.token_hex(6)}@example.org")
    password: str = field(default_factory=lambda: secrets.token_urlsafe(24))

    def password_for(self, form: Form) -> Dict[str, str]:
        """Return the password for the fields of a form that choose one; nothing where it asks for none.

        Args:
            form: The form.
        """
        return {name: self.password for name in ("password", "password-new", "password-confirm") if name in form.fields}


class Check:
    """The steps, each reported with one line."""

    def __init__(self, places: Places) -> None:
        """Begin with nothing checked.

        Args:
            places: Where everything is.
        """
        self.places = places
        self.ok = True

    def step(self, name: str, work: Any) -> Any:
        """Run one step and say how it went; a failed one returns ``None``.

        Args:
            name: What is checked.
            work: The step.
        """
        try:
            value = work()
        except (Failed, requests.RequestException, KeyError, ValueError, IndexError) as e:
            self.ok = False
            print(f"failed  {name}: {type(e).__name__ if not isinstance(e, Failed) else ''}{e}")
            return None
        print(f"ok      {name}")
        return value

    # ----------------------------------------------------------------- the application

    def begin(self, browser: Browser) -> Page:
        """Start a sign-in in the application and return Keycloak's page.

        Raises:
            Failed: When the application sends the browser elsewhere.
        """
        start = browser.send("GET", f"{self.places.public_url}/api/auth/login")
        location = start.headers.get("Location", "")
        if start.status_code != 303 or not location.startswith(self.places.issuer + "/"):
            raise Failed(f"the application sent the browser elsewhere ({start.status_code} {urlsplit(location).path or 'nowhere'})")
        answer = browser.follow(browser.send("GET", location), self.places.public_url)
        return Page(answer)

    def arrive(self, browser: Browser, answer: requests.Response) -> str:
        """Come back to the application from Keycloak and return the page it sends the browser to.

        Raises:
            Failed: When Keycloak does not send the browser back.
        """
        location = urljoin(answer.url, answer.headers.get("Location", ""))
        if not location.startswith(f"{self.places.public_url}/api/auth/callback?"):
            raise Failed(f"Keycloak does not send the browser back: {Page(answer).heading() if answer.text else location}")
        return browser.send("GET", location).headers.get("Location", "")

    def me(self, browser: Browser) -> Optional[Dict[str, Any]]:
        """Return what the application says of the browser's session; ``None`` without one.

        Raises:
            Failed: When it answers neither: an error is no sign-out.
        """
        answer = browser.send("GET", f"{self.places.public_url}/api/auth/me")
        if answer.status_code == 401:
            return None
        if answer.status_code != 200:
            raise Failed(f"the application answered {answer.status_code} for the session")
        return dict(answer.json())

    # ----------------------------------------------------------------- Keycloak's administration

    def administration(self) -> Dict[str, str]:
        """Sign the administrator in and return the header of its requests.

        Raises:
            Failed: When it cannot sign in.
        """
        answer = Browser().send("POST", f"{self.places.keycloak}/realms/master/protocol/openid-connect/token", data={
            "grant_type": "password", "client_id": "admin-cli", "username": self.places.admin, "password": self.places.password,
        })
        if answer.status_code != 200:
            raise Failed(f"the administrator cannot sign in ({answer.status_code})")
        return {"Authorization": f"Bearer {answer.json()['access_token']}"}

    def administer(self, method: str, path: str, body: Any = None) -> requests.Response:
        """Send one request to the realm's administration.

        Raises:
            Failed: When it is refused.
        """
        answer = Browser().send(method, f"{self.places.realm}{path}", json=body, headers=self.administration())
        if answer.status_code >= 400:
            raise Failed(f"Keycloak's administration answered {answer.status_code} to {method} {path or 'the realm'}")
        return answer

    # ----------------------------------------------------------------- the steps

    def oidc_check(self) -> None:
        """Let ``oidc check`` look at the provider, as on a hosted one.

        Raises:
            Failed: When a line of it failed.
        """
        sys.path.insert(0, str(BACKEND))
        from app.core import config
        from app.oidc import check

        for name, value in {
            "environment_raw": "development", "oidc_issuer_raw": self.places.issuer, "oidc_client_id_raw": self.places.client_id,
            "public_url_raw": self.places.public_url, "oidc_post_logout_raw": "",
        }.items():
            setattr(config.settings, name, value)
        refused = [f"{self.places.public_url}/", f"{self.places.public_url}/api/auth/callback/x", "http://other.localhost/api/auth/callback"]
        if not check.run(pkce=True, refused_callbacks=refused, write=lambda line: print(f"        | {line}")):
            raise Failed("a line of it failed")

    def mail_link(self, person: Person) -> str:
        """Wait for the mail that confirms an address and return its link.

        Raises:
            Failed: When none comes.
        """
        until = time.monotonic() + SECONDS
        while time.monotonic() < until:
            found = Browser().send("GET", f"{self.places.mail}/api/v1/search", params={"query": f"to:{person.email}"}).json()["messages"]
            if found:
                text = Browser().send("GET", f"{self.places.mail}/api/v1/message/{found[0]['ID']}").json()["Text"]
                links = re.findall(rf"{re.escape(self.places.keycloak)}/\S*action-token\S*", text)
                if links:
                    return str(links[0])
            time.sleep(0.3)
        raise Failed("no mail with a link came")

    def register(self, browser: Browser, person: Person) -> None:
        """Register, confirm the address with the mail's link, choose a password, and come back signed in.

        Keycloak asks for the password on one of its pages: in the first form, or once the address is confirmed.

        Raises:
            Failed: When a step of it does not do what it should.
        """
        page = self.begin(browser)
        links = [link for link in page.links if "/login-actions/registration" in link]
        if not links:
            raise Failed(f"Keycloak's page offers no registration: {page.heading()}")
        form = Page(browser.send("GET", links[0])).form_with("email")
        answer = browser.submit(form, {"email": person.email, "firstName": "Test", "lastName": "Person", **person.password_for(form)})
        answer = browser.follow(answer, self.places.public_url)
        if answer.is_redirect:
            raise Failed("the sign-in went on before the address was confirmed")
        answer = browser.follow(browser.send("GET", self.mail_link(person)), self.places.public_url)
        for _ in range(4):  # pages that ask for the password, or only to go on
            if answer.is_redirect:
                break
            page = Page(answer)
            asking = [form for form in page.forms if person.password_for(form)]
            onward = [link for link in page.links if "/login-actions/" in link or "/protocol/openid-connect/auth" in link]
            if not asking and not onward:
                raise Failed(f"the mail's link does not lead on: {page.heading()}")
            answer = browser.submit(asking[0], person.password_for(asking[0])) if asking else browser.send("GET", onward[0])
            answer = browser.follow(answer, self.places.public_url)
        target = self.arrive(browser, answer)
        me = self.me(browser)
        if target != "/" or not me or me["sign_in"] != "rohub" or me["user"]["email"] != person.email:
            raise Failed(f"nobody is signed in (sent to {target})")

    def sign_in(self, browser: Browser, person: Person) -> str:
        """Sign in with the password and return the page the application sends the browser to."""
        page = self.begin(browser)
        answer = browser.submit(page.form_with("username", "password"), {"username": person.email, "password": person.password})
        return self.arrive(browser, browser.follow(answer, self.places.public_url))

    def refuse_unverified(self) -> None:
        """Let Keycloak sign in somebody whose address it did not verify; the application must refuse.

        Raises:
            Failed: When it does not.
        """
        person, browser = Person(), Browser()
        self.administer("PUT", "", {"verifyEmail": False})
        try:
            self.administer("POST", "/users", {
                "username": person.email, "email": person.email, "firstName": "Test", "lastName": "Person", "enabled": True, "emailVerified": False,
                "credentials": [{"type": "password", "value": person.password, "temporary": False}],
            })
            target = self.sign_in(browser, person)
        finally:
            self.administer("PUT", "", {"verifyEmail": True})
        if parse_qs(urlsplit(target).query).get("error") != ["email_not_verified"] or self.me(browser) is not None:
            raise Failed(f"it was not refused as it should be (sent to {target})")

    def sign_out(self, browser: Browser, person: Person) -> None:
        """Sign out in the application and then at Keycloak; afterwards a new sign-in asks for the password again.

        Raises:
            Failed: When a session goes on.
        """
        me = self.me(browser)
        if not me:
            raise Failed("nobody is signed in")
        headers = {"X-CSRF-Token": me["csrf_token"], "Origin": self.places.public_url}
        answer = browser.send("POST", f"{self.places.public_url}/api/auth/logout", headers=headers)
        address = answer.json().get("end_session_url") if answer.status_code == 200 else None
        if not address or self.me(browser) is not None:
            raise Failed(f"the session in the application has not ended ({answer.status_code})")
        there = browser.send("GET", urljoin(self.places.public_url, address))
        location = there.headers.get("Location", "")
        if not location.startswith(f"{self.places.issuer}/protocol/openid-connect/logout?"):
            raise Failed(f"the application does not lead to Keycloak's sign-out ({there.status_code})")
        home = f"{self.places.public_url}/"
        answer = browser.follow(browser.send("GET", location), home)
        if not answer.is_redirect:  # Keycloak asks first, since the address carries no token
            answer = browser.follow(browser.submit(Page(answer).form_with("confirmLogout")), home)
        came_back = answer.is_redirect and urljoin(answer.url, answer.headers["Location"]) == home
        print(f"        the browser {'came back to the main page' if came_back else 'stayed on Keycloak’s page, as without IADOPT_OIDC_POST_LOGOUT'}")
        self.begin(browser).form_with("username", "password")

    def provider_sign_out(self, person: Person) -> None:
        """Sign in again and let Keycloak end the session by itself.

        Raises:
            Failed: When the application's session goes on.
        """
        browser = Browser()
        if self.sign_in(browser, person) != "/" or self.me(browser) is None:
            raise Failed("nobody is signed in")
        [user] = self.administer("GET", f"/users?exact=true&email={person.email.replace('@', '%40')}").json()
        self.administer("POST", f"/users/{user['id']}/logout")
        until = time.monotonic() + SECONDS
        while time.monotonic() < until:
            if self.me(browser) is None:
                return
            time.sleep(0.3)
        raise Failed("the application's session goes on")

    def run(self) -> bool:
        """Run every step and return whether all went well."""
        person, browser = Person(), Browser()
        self.step("oidc check, with PKCE and refused callbacks", self.oidc_check)
        self.step("somebody who registered and confirmed the address signs in", lambda: self.register(browser, person))
        self.step("an address that is not verified is refused", self.refuse_unverified)
        self.step("signing out ends the session here and there", lambda: self.sign_out(browser, person))
        self.step("a session that Keycloak ends is ended in the application", lambda: self.provider_sign_out(person))
        return self.ok


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the check: 0 when every step went well, 1 when one failed, 2 when it was not begun.

    Args:
        argv: The arguments; the process's by default.
    """
    parser = argparse.ArgumentParser(description="Try the sign-in against the local Keycloak. Everything must be on this machine.")
    parser.add_argument("--issuer", default="http://keycloak.localhost:8180/realms/iadopt-local")
    parser.add_argument("--client-id", default="iadopt-local")
    parser.add_argument("--public-url", default="http://localhost:5173", help="where the application is, as IADOPT_PUBLIC_URL")
    parser.add_argument("--mail", default="http://localhost:8125", help="the mail catcher's page")
    arguments = parser.parse_args(argv)
    addresses = {"the issuer": arguments.issuer, "the application": arguments.public_url, "the mail catcher": arguments.mail}
    for name, url in addresses.items():
        if not is_this_machine(url):
            print(f"Nothing was sent: {name} is not on this machine ({url}).", file=sys.stderr)
            return 2
    admin, password = os.environ.get("KEYCLOAK_ADMIN_USERNAME"), os.environ.get("KEYCLOAK_ADMIN_PASSWORD")
    if not admin or not password:
        print("Nothing was sent: KEYCLOAK_ADMIN_USERNAME and KEYCLOAK_ADMIN_PASSWORD name the administrator.", file=sys.stderr)
        return 2
    stay_on_this_machine()
    places = Places(arguments.issuer.rstrip("/"), arguments.client_id, arguments.public_url.rstrip("/"), arguments.mail.rstrip("/"), admin, password)
    return 0 if Check(places).run() else 1


if __name__ == "__main__":
    sys.exit(main())
