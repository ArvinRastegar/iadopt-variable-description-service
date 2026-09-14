"""Tests for the second-call label/comment generator.

Contract: docs/components/label-comment.md — D-002.
Stubbed at the provider helper seam; no network.
"""

from __future__ import annotations

import pytest

from app.services import label_comment as lc

DEFN = "Mass concentration of dissolved organic carbon in water"
CALL_KWARGS = dict(model_provider="psnc", model="Qwen3.8-27B", temperature=0.5)


@pytest.fixture
def stub_call(monkeypatch):
    """Replace the PSNC helper and record how many times it was called."""
    calls = []

    def make(response=None, raises=None):
        def fake(model, prompt, temperature, disable_thinking=True):
            calls.append(prompt)
            if raises is not None:
                raise raises
            return response

        monkeypatch.setattr(lc, "call_psnc_model", fake, raising=False)
        return calls

    return make


def test_well_formed_response_is_used(stub_call):
    stub_call('{"label": "DOC concentration", "comment": "Dissolved organic carbon in water."}')
    assert lc.generate_label_and_comment(DEFN, **CALL_KWARGS) == (
        "DOC concentration", "Dissolved organic carbon in water."
    )


def test_fenced_response_is_parsed(stub_call):
    stub_call('```json\n{"label": "L", "comment": "C"}\n```')
    assert lc.generate_label_and_comment(DEFN, **CALL_KWARGS) == ("L", "C")


def test_empty_response_falls_back(stub_call):
    stub_call("")
    assert lc.generate_label_and_comment(DEFN, **CALL_KWARGS) == lc.fallback_label_and_comment(DEFN)


def test_malformed_json_falls_back(stub_call):
    stub_call("not json at all")
    assert lc.generate_label_and_comment(DEFN, **CALL_KWARGS) == lc.fallback_label_and_comment(DEFN)


def test_missing_label_falls_back_per_field(stub_call):
    stub_call('{"comment": "A good comment."}')
    label, comment = lc.generate_label_and_comment(DEFN, **CALL_KWARGS)
    assert comment == "A good comment."
    assert label == lc.fallback_label_and_comment(DEFN)[0]


@pytest.mark.parametrize("bad", ['""', '"   "', "123", "{}"])
def test_unusable_label_falls_back(stub_call, bad):
    stub_call('{"label": %s, "comment": "C"}' % bad)
    label, comment = lc.generate_label_and_comment(DEFN, **CALL_KWARGS)
    assert comment == "C"
    assert label == lc.fallback_label_and_comment(DEFN)[0]


def test_exception_from_the_call_never_propagates(stub_call):
    stub_call(raises=RuntimeError("transport exploded"))
    assert lc.generate_label_and_comment(DEFN, **CALL_KWARGS) == lc.fallback_label_and_comment(DEFN)


def test_provider_helper_is_called_exactly_once(stub_call):
    calls = stub_call('{"label": "L", "comment": "C"}')
    lc.generate_label_and_comment(DEFN, **CALL_KWARGS)
    assert len(calls) == 1


def test_overlong_model_label_is_truncated(stub_call):
    stub_call('{"label": "%s", "comment": "C"}' % ("word " * 40))
    label, _ = lc.generate_label_and_comment(DEFN, **CALL_KWARGS)
    assert len(label) <= lc.MAX_LABEL_CHARS


@pytest.mark.parametrize("bad", ["", "   ", "\n"])
def test_empty_definition_raises(bad):
    with pytest.raises(ValueError):
        lc.generate_label_and_comment(bad, **CALL_KWARGS)
    with pytest.raises(ValueError):
        lc.fallback_label_and_comment(bad)


def test_fallback_is_deterministic():
    assert lc.fallback_label_and_comment(DEFN) == lc.fallback_label_and_comment(DEFN)


def test_fallback_for_short_definition_is_the_definition():
    label, comment = lc.fallback_label_and_comment(DEFN)
    assert label == DEFN
    assert comment == DEFN


def test_fallback_for_long_definition_is_bounded_and_clean():
    long = ("a statistical unit which is defined as a grouping of homogeneous "
            "neighboring building blocks in an urban area, measured annually. ") * 3
    label, comment = lc.fallback_label_and_comment(long)
    assert len(label) <= lc.MAX_LABEL_CHARS
    assert label == label.strip()
    assert not label.endswith((",", ".", ";", ":"))
    assert comment == long


def test_prompt_inserts_the_definition_in_one_pass():
    prompt = lc.build_label_comment_prompt("{definition}")
    assert prompt.count("{definition}") == 1
