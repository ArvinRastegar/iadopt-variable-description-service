"""The switch that keeps the best pipeline's extra requests out of the tests written before it.

Those tests script a provider's answers request by request, one decomposition answer at a time. ``conftest.py``
applies this switch to every test module but ``test_best_pipeline.py``, which checks that the pipeline goes through
exactly what it switches off: the measured prompt then shows today's 25 examples and states no matrix, without a request
or a recorded call, and the fill prompt goes out once, as before.
"""

from __future__ import annotations

import importlib
import sys
from typing import Any

import pytest

BEST_PIPELINE_TESTS = "test_best_pipeline"


class _NoNetwork:
    """A session that refuses every request: nothing reaches PSNC from a switched-off step."""

    def post(self, url: str, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"No test outside test_best_pipeline.py may send a request to {url}.")


def switch_best_pipeline_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Today's 25 examples without a request, no matrix finder, one fill answer.

    ``retrieve_examples`` gives ``fallback_examples`` with the same leave-out set, ``find_matrix`` states nothing,
    ``answer_vote.ANSWERS`` is 1, wherever the application holds them; the whole application is imported first, so no
    module copies them later. The two modules' own HTTP sessions refuse every request.

    Args:
        monkeypatch: The test's monkeypatch, which undoes all of it.
    """
    importlib.import_module("app.main")
    retrieval = importlib.import_module("app.services.example_retrieval")
    finder = importlib.import_module("app.services.matrix_finder")
    vote = importlib.import_module("app.services.answer_vote")

    def fallback(*args: Any, leave_out: Any = None, **kwargs: Any) -> Any:
        return retrieval.fallback_examples(leave_out=frozenset(leave_out or ()))

    def no_finder(*args: Any, **kwargs: Any) -> Any:
        return finder.NO_FINDING

    replacements = {"retrieve_examples": (retrieval.retrieve_examples, fallback),
                    "find_matrix": (finder.find_matrix, no_finder)}
    for name, module in list(sys.modules.items()):
        if module is None or not (name == "app" or name.startswith("app.")):
            continue
        for attribute, (original, replacement) in replacements.items():
            if getattr(module, attribute, None) is original:
                monkeypatch.setattr(module, attribute, replacement)
    monkeypatch.setattr(vote, "ANSWERS", 1)
    for module in (retrieval, finder):
        monkeypatch.setattr(module, "get_http_session", lambda: _NoNetwork())
