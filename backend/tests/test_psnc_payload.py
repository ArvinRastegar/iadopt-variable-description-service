"""The request bodies sent to PSNC's chat-completions API are exactly the measured ones."""

from __future__ import annotations

import json

from app.clients import psnc_client

MEASURED_BODY = {
    "model": "Qwen3.8-27B",
    "messages": [{"role": "user", "content": "PROMPT"}],
    "stream": False,
    "temperature": 0.5,
    "top_p": 1.0,
    "max_tokens": 16000,
    "chat_template_kwargs": {"enable_thinking": False},
}


def build(**overrides):
    kwargs = dict(temperature=0.5, top_p=1.0, max_tokens=16000)
    kwargs.update(overrides)
    return psnc_client.build_measured_psnc_payload("Qwen3.8-27B", "PROMPT", **kwargs)


def test_the_decomposition_body_is_the_measured_one():
    assert build() == MEASURED_BODY
    json.dumps(build())


def test_streaming_changes_only_the_stream_flag():
    assert build(stream=True) == {**MEASURED_BODY, "stream": True}


def test_the_entity_link_body_asks_for_one_greedy_token_without_log_probabilities():
    payload = psnc_client.build_entity_link_payload(
        model="Qwen3.8-27B", system_prompt="SYSTEM", user_message="term: α-diversity\n"
    )
    assert payload == {
        "model": "Qwen3.8-27B",
        "messages": [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "term: α-diversity\n"},
        ],
        "stream": False,
        "temperature": 0,
        "max_tokens": 1,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    json.dumps(payload)


def test_the_legacy_body_keeps_both_thinking_switches_and_no_sampling_limits():
    assert psnc_client.build_psnc_chat_payload("m", "p", 0.5) == {
        "model": "m",
        "temperature": 0.5,
        "messages": [{"role": "user", "content": "p"}],
        "stream": False,
        "enable_thinking": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }


def test_requests_go_to_the_v1_chat_completions_endpoint():
    assert psnc_client.psnc_chat_completions_url().endswith("/v1/chat/completions")
