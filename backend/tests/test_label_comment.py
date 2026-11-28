"""The second call that supplies a label and a comment, with a fallback that never fails a decomposition (D-002).

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
        def fake(model, prompt, temperature, disable_thinking=True, **_call_log):
            calls.append(prompt)
            if raises is not None:
                raise raises
            return response

        monkeypatch.setattr(lc, "call_psnc_model", fake, raising=False)
        return calls

    return make


def test_a_well_formed_answer_is_used_after_one_request(stub_call):
    calls = stub_call('{"label": "DOC concentration", "comment": "Dissolved organic carbon in water."}')
    assert lc.generate_label_and_comment(DEFN, **CALL_KWARGS) == (
        "DOC concentration", "Dissolved organic carbon in water."
    )
    assert len(calls) == 1


def test_a_fenced_answer_is_read(stub_call):
    stub_call('```json\n{"label": "L", "comment": "C"}\n```')
    assert lc.generate_label_and_comment(DEFN, **CALL_KWARGS) == ("L", "C")


@pytest.mark.parametrize(
    "response, raises",
    [("", None), ("not json at all", None), (None, RuntimeError("transport exploded"))],
    ids=["empty", "not-json", "call-fails"],
)
def test_an_unusable_answer_or_a_failed_call_falls_back_without_failing(stub_call, response, raises):
    stub_call(response, raises=raises)
    assert lc.generate_label_and_comment(DEFN, **CALL_KWARGS) == (DEFN, DEFN)


@pytest.mark.parametrize("answer", ['{"comment": "C"}', '{"label": "   ", "comment": "C"}'])
def test_a_missing_or_blank_label_falls_back_for_that_field_only(stub_call, answer):
    stub_call(answer)
    assert lc.generate_label_and_comment(DEFN, **CALL_KWARGS) == (DEFN, "C")


def test_an_overlong_model_label_is_cut(stub_call):
    stub_call('{"label": "%s", "comment": "C"}' % ("word " * 40))
    label, _ = lc.generate_label_and_comment(DEFN, **CALL_KWARGS)
    assert len(label) <= lc.MAX_LABEL_CHARS


def test_an_empty_definition_is_refused():
    with pytest.raises(ValueError):
        lc.generate_label_and_comment(" \n", **CALL_KWARGS)
    with pytest.raises(ValueError):
        lc.fallback_label_and_comment(" \n")


def test_the_fallback_label_of_a_long_definition_is_bounded_and_clean():
    long = ("a statistical unit which is defined as a grouping of homogeneous "
            "neighboring building blocks in an urban area, measured annually. ") * 3
    label, comment = lc.fallback_label_and_comment(long)
    assert len(label) <= lc.MAX_LABEL_CHARS
    assert label == label.strip()
    assert not label.endswith((",", ".", ";", ":"))
    assert comment == long


def test_the_prompt_inserts_the_definition_in_one_pass():
    assert lc.build_label_comment_prompt("{definition}").count("{definition}") == 1
