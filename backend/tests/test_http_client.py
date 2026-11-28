"""The shared outbound HTTP session identifies the service, as Wikimedia asks of clients (D-032). Fully offline."""

from __future__ import annotations

from app.clients import http


def test_the_user_agent_names_the_client_and_a_contact_as_a_plain_header_value():
    agent = http.USER_AGENT
    assert agent.startswith("IADOPT-Linker/")
    assert "https://github.com/ArvinRastegar/iadopt-variable-description-service" in agent
    agent.encode("ascii")
    assert "\n" not in agent and "\r" not in agent and agent == agent.strip()


def test_one_shared_session_sends_it(monkeypatch):
    monkeypatch.setattr(http, "_http_session", None)
    session = http.get_http_session()
    assert session.headers["User-Agent"] == http.USER_AGENT
    assert http.get_http_session() is session
