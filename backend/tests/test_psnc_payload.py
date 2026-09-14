"""Tests for the measured PSNC request body.

Contract: docs/components/psnc-payload.md
"""

from __future__ import annotations

import json

from app.clients import psnc_client

MEASURED_KEYS = {"model", "messages", "stream", "temperature", "top_p", "max_tokens", "chat_template_kwargs"}


def build(**overrides):
    kwargs = dict(temperature=0.5, top_p=1.0, max_tokens=16000)
    kwargs.update(overrides)
    return psnc_client.build_measured_psnc_payload("Qwen3.8-27B", "PROMPT", **kwargs)


def test_payload_has_exactly_the_seven_measured_keys():
    assert set(build()) == MEASURED_KEYS


def test_payload_has_no_top_level_enable_thinking():
    assert "enable_thinking" not in build()


def test_chat_template_kwargs_disables_thinking():
    assert build()["chat_template_kwargs"] == {"enable_thinking": False}


def test_single_user_message_with_no_system_message():
    assert build()["messages"] == [{"role": "user", "content": "PROMPT"}]


def test_measured_sampling_values_pass_through():
    payload = build()
    assert payload["temperature"] == 0.5
    assert payload["top_p"] == 1.0
    assert payload["max_tokens"] == 16000


def test_stream_defaults_false_and_changes_nothing_else():
    non_streamed, streamed = build(), build(stream=True)
    assert non_streamed["stream"] is False
    assert streamed["stream"] is True
    assert {k: v for k, v in streamed.items() if k != "stream"} == {
        k: v for k, v in non_streamed.items() if k != "stream"
    }


def test_payload_is_json_serializable():
    json.dumps(build())


def test_legacy_builder_is_unchanged():
    """Regression: the legacy path must not have been touched."""
    legacy = psnc_client.build_psnc_chat_payload("m", "p", 0.5)
    assert legacy["enable_thinking"] is False
    assert legacy["chat_template_kwargs"] == {"enable_thinking": False}
    assert "top_p" not in legacy
    assert "max_tokens" not in legacy


def test_chat_completions_url_targets_v1():
    assert psnc_client.psnc_chat_completions_url().endswith("/v1/chat/completions")
