"""The release quality gate's own logic (D-043), without the lab and without a model.

The lab's scorer and corpus, and the backend container, are replaced by fakes: these tests
check how the gate counts answers, decides, reports and talks to the container. The real
scorer is the lab's, run by the gate itself with the lab's Python.
"""

import importlib.util
import io
import json
import pathlib
import sys
from types import SimpleNamespace

import pytest

TOOLS = pathlib.Path(__file__).resolve().parents[1] / "tools"


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


qg = load_tool("quality_gate")
container = load_tool("quality_gate_container")


def make_records(count=102):
    return [{"variable_id": f"v{i:03d}", "definition": f"definition {i}", "gold": {"hasProperty": f"p{i}"}}
            for i in range(count)]


def fake_scorer():
    """Scores 1 for an exact answer and 0 otherwise; F1 is the share of 1s."""

    def extract(text):
        try:
            return SimpleNamespace(success=True, candidate=json.loads(text))
        except ValueError:
            return SimpleNamespace(success=False, candidate=None)

    def aggregate(evidence, ids):
        assert [item["variable_id"] for item in evidence] == ids
        share = sum(item["hit"] for item in evidence) / len(evidence) if evidence else 0.0
        return {"f1": share, "precision": share, "recall": share}

    return qg.Scorer(
        extract=extract,
        validate=lambda candidate: SimpleNamespace(valid=isinstance(candidate, dict) and "hasProperty" in candidate),
        evaluate=lambda gold, prediction, variable_id: {"variable_id": variable_id, "hit": int(prediction == gold)},
        empty=dict,
        aggregate=aggregate,
    )


def answer(record):
    return {"kind": "prediction", "variable_id": record["variable_id"], "ok": True,
            "raw_output": json.dumps(record["gold"]), "error": None, "seconds": 1.0, "decomposition_tries": 1}


RUN = {"kind": "run", "provider": "psnc", "model": "Qwen3.8-27B", "prompt_setup": "measured", "prompt_sha256": "b0a1" * 16}


def test_the_container_output_is_read_and_log_lines_are_ignored():
    lines = ["INFO starting\n", json.dumps(RUN) + "\n", "not json\n", "[1, 2]\n",
             json.dumps({"kind": "prediction", "variable_id": "v000", "ok": True, "raw_output": "{}"}) + "\n"]
    run, predictions = qg.parse_container_output(lines)
    assert run["model"] == "Qwen3.8-27B"
    assert list(predictions) == ["v000"]
    with pytest.raises(qg.GateError):
        qg.parse_container_output(["no run line\n"])


def test_failed_missing_and_unusable_answers_count_as_empty_predictions():
    records = make_records(6)
    predictions = {r["variable_id"]: answer(r) for r in records[:2]}
    predictions["v002"] = {**answer(records[2]), "ok": False, "raw_output": None, "error": "HTTP 401"}
    predictions["v003"] = {**answer(records[3]), "raw_output": "not json at all"}
    predictions["v004"] = {**answer(records[4]), "raw_output": json.dumps({"something": "else"})}
    # v005 never came back.
    result = qg.score(records, predictions, fake_scorer())
    assert [row["outcome"] for row in result["rows"]] == ["scored", "scored", "failed", "unusable", "unusable", "missing"]
    assert result["counts"] == {"scored": 2, "failed": 1, "unusable": 2, "missing": 1}
    assert result["official"]["f1"] == pytest.approx(2 / 6)  # every record counts, as the lab reports
    assert result["as_run"]["f1"] == pytest.approx(1.0)      # usable answers only
    assert result["rows"][2]["error"] == "HTTP 401"


def run_main(tmp_path, records, predictions, *extra):
    def load(lab):
        return records, fake_scorer()

    def predict(items, concurrency, progress=None):
        assert [item["variable_id"] for item in items] == [r["variable_id"] for r in records]
        assert concurrency == 8
        for done in range(1, len(predictions) + 1):
            progress(done)
        return RUN, predictions

    code = qg.main(["--lab", str(tmp_path), "--report-dir", str(tmp_path / "reports"), *extra], load=load, predict=predict)
    reports = sorted((tmp_path / "reports").glob("quality-gate-*.json"))
    return code, json.loads(reports[-1].read_text()) if reports else None


def test_the_gate_passes_at_the_threshold_and_fails_below_it(tmp_path, capsys):
    records = make_records()
    predictions = {r["variable_id"]: answer(r) for r in records[:62]}  # 62 of 102 right: 0.6078
    code, report = run_main(tmp_path, records, predictions)
    assert (code, report["verdict"]) == (qg.EXIT_PASS, "pass")
    assert report["official"]["f1"] == pytest.approx(62 / 102)
    assert report["run"]["provider"] == "psnc" and report["threshold"] == 0.60
    assert "Threshold 0.60: PASS" in capsys.readouterr().out

    predictions = {r["variable_id"]: answer(r) for r in records[:61]}  # 61 of 102: 0.5980
    code, report = run_main(tmp_path, records, predictions)
    assert (code, report["verdict"]) == (qg.EXIT_FAIL, "fail")
    assert "Threshold 0.60: FAIL" in capsys.readouterr().out

    code, _ = run_main(tmp_path, records, {r["variable_id"]: answer(r) for r in records}, "--threshold", "1.0")
    assert code == qg.EXIT_PASS  # exactly at the threshold passes


def test_the_gate_cannot_run_without_the_lab_or_with_another_corpus(tmp_path, capsys):
    def no_lab(lab):
        raise qg.GateError("run this with the lab's Python")

    assert qg.main(["--lab", str(tmp_path)], load=no_lab) == qg.EXIT_ERROR
    assert "QUALITY GATE COULD NOT RUN" in capsys.readouterr().err
    code, report = run_main(tmp_path, make_records(101), {})
    assert (code, report) == (qg.EXIT_ERROR, None)


class FakeProcess:
    def __init__(self, lines, code):
        self.stdin = io.StringIO()
        self.stdin.close = lambda: None  # keep the written text readable for the test
        self.stdout = iter(lines)
        self.code = code

    def wait(self):
        return self.code


def test_the_definitions_go_to_the_backend_container(tmp_path):
    started = {}
    lines = [json.dumps(RUN) + "\n", json.dumps(answer(make_records(1)[0])) + "\n"]

    def popen(command, **kwargs):
        started["command"], started["cwd"] = command, kwargs["cwd"]
        started["process"] = FakeProcess(lines, 0)
        return started["process"]

    seen = []
    run, predictions = qg.run_predictions([{"variable_id": "v000", "definition": "d"}], 4, popen=popen,
                                          cwd=tmp_path, progress=seen.append)
    command = started["command"]
    assert command[:7] == ["docker", "compose", "exec", "-T", "backend", "python", "-c"]
    assert command[7] == (TOOLS / "quality_gate_container.py").read_text(encoding="utf-8")
    assert command[8] == "4"
    assert json.loads(started["process"].stdin.getvalue()) == [{"variable_id": "v000", "definition": "d"}]
    assert (run["model"], list(predictions), seen) == ("Qwen3.8-27B", ["v000"], [1])

    with pytest.raises(qg.GateError, match="exited with 1"):
        qg.run_predictions([], 4, popen=lambda command, **kwargs: FakeProcess([], 1), cwd=tmp_path)


def test_the_container_program_reports_the_answer_or_the_error(monkeypatch):
    from app import pipeline
    from app.services.call_log import start_call

    def answered(definition, calls=None):
        start_call(calls, "decomposition", "psnc", "Qwen3.8-27B").finish("ok", usage={"prompt_tokens": 5208,
                                                                                       "completion_tokens": 113})
        return {"raw_llm_output": '{"hasProperty": "count"}'}

    monkeypatch.setattr(pipeline, "run_pipeline", answered)
    line = container.decompose_one({"variable_id": "v1", "definition": "d"})
    assert (line["ok"], line["raw_output"], line["error"]) == (True, '{"hasProperty": "count"}', None)
    assert (line["decomposition_tries"], line["prompt_tokens"]) == (1, 5208)

    def broken(definition, calls=None):
        raise RuntimeError("PSNC refused the key")

    monkeypatch.setattr(pipeline, "run_pipeline", broken)
    line = container.decompose_one({"variable_id": "v1", "definition": "d"})
    assert (line["ok"], line["raw_output"], line["error"]) == (False, None, "RuntimeError: PSNC refused the key")

    monkeypatch.setattr(pipeline, "run_pipeline", lambda definition, calls=None: {"raw_llm_output": ""})
    line = container.decompose_one({"variable_id": "v1", "definition": "d"})
    assert (line["ok"], line["error"]) == (False, "The pipeline returned no answer.")


def test_the_container_program_writes_the_run_line_first_and_keeps_the_order(monkeypatch, capsys):
    monkeypatch.setattr(container, "run_description", lambda: dict(RUN))
    monkeypatch.setattr(container, "decompose_one",
                        lambda item: {"variable_id": item["variable_id"], "ok": True, "raw_output": "{}"})
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps([{"variable_id": f"v{i}", "definition": "d"} for i in range(5)])))
    container.main(3)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[0]["kind"] == "run"
    assert [line["variable_id"] for line in lines[1:]] == [f"v{i}" for i in range(5)]
