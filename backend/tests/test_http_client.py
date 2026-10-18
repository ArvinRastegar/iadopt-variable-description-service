"""Tests for the shared outbound HTTP session and the User-Agent it sends.

Wikimedia throttles generic agents hard and asks clients to identify themselves with a
name, a version and a contact; see the module docstring of app/clients/http.py and
``decisions.md`` D-032. Fully offline.
"""

from __future__ import annotations

import pytest

from app.clients import http


@pytest.fixture
def fresh_session(monkeypatch):
    monkeypatch.setattr(http, "_http_session", None)


def test_user_agent_names_the_client_and_gives_a_contact_url():
    agent = http.USER_AGENT
    assert agent.startswith("IADOPT-Linker/")
    assert "https://github.com/ArvinRastegar/iadopt-variable-description-service" in agent


def test_user_agent_is_no_longer_the_generic_one_wikimedia_throttled():
    assert http.USER_AGENT != "IADOPT-Linker/1.0 (+fastapi)"
    assert "+fastapi" not in http.USER_AGENT


def test_user_agent_is_a_valid_plain_header_value():
    http.USER_AGENT.encode("ascii")
    assert "\n" not in http.USER_AGENT and "\r" not in http.USER_AGENT
    assert http.USER_AGENT == http.USER_AGENT.strip()


def test_the_session_sends_it_and_is_shared(fresh_session):
    session = http.get_http_session()
    assert session.headers["User-Agent"] == http.USER_AGENT
    assert http.get_http_session() is session
