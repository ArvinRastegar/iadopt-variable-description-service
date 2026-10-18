"""Keep the two deployed nginx access policies aligned.

These source checks cover the access boundary in both deployment manifests;
they do not replace an nginx runtime check.
"""

from pathlib import Path
import re
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[2]
STANDALONE = (ROOT / "frontend/nginx.conf").read_text()
OPENSHIFT = textwrap.dedent(
    (ROOT / "deploy/openshift/iadopt.yaml")
    .read_text()
    .split("  default.conf: |\n", 1)[1]
    .split("\n---", 1)[0]
)
CONFIGS = [pytest.param(STANDALONE, id="docker"), pytest.param(OPENSHIFT, id="openshift")]


def _locations(config: str) -> dict[str, str]:
    return {
        selector.strip(): body
        for selector, body in re.findall(r"^\s*location\s+([^\n{]+)\{([^{}]*)\}", config, re.M)
    }


@pytest.mark.parametrize("config", CONFIGS)
def test_admin_page_is_the_only_authenticated_static_location(config: str):
    locations = _locations(config)
    admin = locations["= /admin.html"]
    assert "auth_request /api/auth/verify;" in admin
    assert "error_page 401 = @login_redirect;" in admin
    assert "try_files /admin.html =404;" in admin
    assert "return 302 /login.html?redirect=$request_uri;" in locations["@login_redirect"]

    # A friendly admin URL must lead back through the protected exact location.
    assert "return 302 /admin.html;" in locations["= /admin"]
    assert config.count("auth_request ") == 1


@pytest.mark.parametrize("config", CONFIGS)
def test_tools_assets_and_login_remain_public(config: str):
    locations = _locations(config)
    public = locations["/"]
    assert "auth_request" not in public
    assert "try_files $uri $uri/ /index.html;" in public
    assert "auth_request" not in locations["= /login.html"]
    assert "try_files /login.html =404;" in locations["= /login.html"]
    assert "index index.html;" in config


@pytest.mark.parametrize("config", CONFIGS)
def test_api_proxy_leaves_authorization_to_backend(config: str):
    locations = _locations(config)
    for selector in (
        "= /api/auth/login",
        "= /api/auth/verify",
        "~ ^/api/(livez|readyz|health)$",
        "/api/",
    ):
        assert "proxy_pass " in locations[selector]
        assert "auth_request" not in locations[selector]
    assert "proxy_buffering off;" in locations["/api/"]
    assert "proxy_read_timeout 900s;" in locations["/api/"]


def test_deployment_access_policies_match():
    def normalized(config: str) -> str:
        lines = []
        for line in config.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "resolver ", "set $backend_upstream ")):
                continue
            lines.append(line.replace("$backend_upstream", "http://backend:8000"))
        return "\n".join(lines)

    assert normalized(STANDALONE) == normalized(OPENSHIFT)
    assert "resolver 127.0.0.11 valid=10s ipv6=off;" in STANDALONE
    assert "set $backend_upstream http://backend:8000;" in STANDALONE
    assert "proxy_pass $backend_upstream;" in STANDALONE
    assert "proxy_pass http://backend:8000;" in OPENSHIFT
