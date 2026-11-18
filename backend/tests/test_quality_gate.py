"""The release quality gate's own logic (D-043), without the lab and without a model.

The lab's scorer and corpus, and the backend container, are replaced by fakes: these tests
check how the gate counts answers, decides, reports and talks to the container. The real
scorer is the lab's, run by the gate itself with the lab's Python.
"""

import dataclasses
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tempfile
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

CODE = {"sha256": "c0de" * 16, "commit": "91747b4", "uncommitted_changes": True}
RUN = {"kind": "run", **qg.RELEASE_BACKEND, "prompt_sha256": "b0a1" * 16,
       "example_definitions": [f"definition {i}" for i in range(25)], "code_sha256": CODE["sha256"]}
NO_ANSWER = {"ok": False, "provider_failed": True, "raw_output": None,
             "error": "The provider never answered: HTTP 401"}


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
    return {"kind": "prediction", "variable_id": record["variable_id"], "ok": True, "provider_failed": False,
            "raw_output": json.dumps(record["gold"]), "error": None, "seconds": 1.0, "decomposition_tries": 1}


def run_main(tmp_path, records, predictions, *extra, run=RUN, scorer=None):
    """Run the gate with fakes; return its exit code, its report and whether it decomposed."""
    predicted = []

    def predict(items, concurrency, progress=None):
        predicted.append(True)
        assert [item["variable_id"] for item in items] == [r["variable_id"] for r in records]
        assert concurrency == 8
        for done in range(1, len(predictions) + 1):
            progress(done)
        return dict(run), predictions

    report_dir = pathlib.Path(tempfile.mkdtemp(dir=tmp_path))
    code = qg.main(["--lab", str(tmp_path), "--report-dir", str(report_dir), *extra],
                   load=lambda lab: (records, scorer or fake_scorer()), code=lambda: dict(CODE),
                   describe=lambda: dict(run), predict=predict)
    reports = sorted(report_dir.glob("quality-gate-*.json"))
    return SimpleNamespace(code=code, report=json.loads(reports[-1].read_text()) if reports else None,
                           predicted=bool(predicted))


def test_the_container_output_is_read_and_log_lines_are_ignored():
    lines = ["INFO starting\n", json.dumps(RUN) + "\n", "not json\n", "[1, 2]\n",
             json.dumps({"kind": "prediction", "variable_id": "v000", "ok": True, "raw_output": "{}"}) + "\n"]
    run, predictions = qg.parse_container_output(lines)
    assert run["model"] == "Qwen3.8-27B"
    assert list(predictions) == ["v000"]
    with pytest.raises(qg.GateError):
        qg.parse_container_output(["no run line\n"])


def test_every_answer_that_is_not_scored_counts_as_an_empty_prediction():
    records = make_records(7)
    predictions = {r["variable_id"]: answer(r) for r in records[:2]}
    predictions["v002"] = {**answer(records[2]), "ok": False, "raw_output": None, "error": "TypeError: boom"}
    predictions["v003"] = {**answer(records[3]), "raw_output": "not json at all"}
    predictions["v004"] = {**answer(records[4]), "raw_output": json.dumps({"something": "else"})}
    predictions["v005"] = {**answer(records[5]), **NO_ANSWER}
    # v006 never came back.
    result = qg.score(records, predictions, fake_scorer())
    assert [row["outcome"] for row in result["rows"]] == [
        "scored", "scored", "failed", "unusable", "unusable", "no_answer", "missing"]
    assert result["counts"] == {"scored": 2, "failed": 1, "unusable": 2, "no_answer": 1, "missing": 1}
    assert result["official"]["f1"] == pytest.approx(2 / 7)  # every record counts
    assert result["as_run"]["f1"] == pytest.approx(1.0)      # usable answers only
    assert result["rows"][2]["error"] == "TypeError: boom"


def test_the_gate_passes_at_the_threshold_and_fails_below_it(tmp_path, capsys):
    records = make_records()
    result = run_main(tmp_path, records, {r["variable_id"]: answer(r) for r in records[:62]})  # 0.6078
    assert (result.code, result.report["verdict"]) == (qg.EXIT_PASS, "pass")
    assert result.report["official"]["f1"] == pytest.approx(62 / 102)
    assert (result.report["code"], result.report["threshold"]) == (CODE, 0.60)
    assert "Threshold 0.60 on all 102: PASS" in capsys.readouterr().out

    # 61 right and the 41 others answered wrongly: a real failure, not a missing answer.
    predictions = {r["variable_id"]: answer(r) for r in records[:61]}
    predictions.update({r["variable_id"]: {**answer(r), "raw_output": json.dumps({"hasProperty": "wrong"})}
                        for r in records[61:]})
    result = run_main(tmp_path, records, predictions)  # 0.5980
    assert (result.code, result.report["verdict"]) == (qg.EXIT_FAIL, "fail")
    assert "Threshold 0.60 on all 102: FAIL" in capsys.readouterr().out

    result = run_main(tmp_path, records, {r["variable_id"]: answer(r) for r in records}, "--threshold", "1.0")
    assert result.code == qg.EXIT_PASS  # exactly at the threshold passes


def test_unanswered_definitions_can_still_pass_but_never_fail(tmp_path, capsys):
    records = make_records()
    predictions = {r["variable_id"]: answer(r) for r in records[:70]}
    predictions.update({r["variable_id"]: {**answer(r), **NO_ANSWER} for r in records[70:]})
    result = run_main(tmp_path, records, predictions)  # 0.6863 even with 32 counted as empty
    assert (result.code, result.report["verdict"]) == (qg.EXIT_PASS, "pass")

    outage = {r["variable_id"]: {**answer(r), **NO_ANSWER} for r in records}
    result = run_main(tmp_path, records, outage)
    assert (result.code, result.report["verdict"]) == (qg.EXIT_ERROR, "undecided")
    out = capsys.readouterr().out
    assert "102 got no answer from the provider or were lost, e.g. The provider never answered: HTTP 401" in out
    assert "Threshold 0.60 on all 102: CANNOT DECIDE" in out

    lost = {r["variable_id"]: answer(r) for r in records[:50]}  # the other 52 lines never came back
    result = run_main(tmp_path, records, lost)
    assert (result.code, result.report["verdict"], result.report["counts"]["missing"]) == (
        qg.EXIT_ERROR, "undecided", 52)


def test_the_unseen_variables_and_the_examples_are_shown_apart_but_do_not_decide(tmp_path, capsys):
    records = make_records()
    # The 25 examples (definitions 0-24) all right, the 77 others: 40 right.
    result = run_main(tmp_path, records, {r["variable_id"]: answer(r) for r in records[:65]})
    groups = result.report["groups"]
    assert (groups["examples"]["variables"], groups["unseen"]["variables"]) == (25, 77)
    assert groups["examples"]["official"]["f1"] == pytest.approx(1.0)
    assert groups["unseen"]["official"]["f1"] == pytest.approx(40 / 77)
    assert (result.code, result.report["official"]["f1"]) == (qg.EXIT_PASS, pytest.approx(65 / 102))  # all 102 decide
    out = capsys.readouterr().out
    assert "the 77 not in the prompt: 0.5195" in out
    assert "the 25 the prompt shows with their answers: 1.0000" in out


def test_without_known_examples_no_groups_are_scored(tmp_path, capsys):
    records = make_records()
    result = run_main(tmp_path, records, {r["variable_id"]: answer(r) for r in records},
                      run={**RUN, "example_definitions": None})
    assert (result.code, result.report["groups"]) == (qg.EXIT_PASS, None)
    assert "examples are unknown" in capsys.readouterr().out


def test_the_gate_cannot_run_without_the_lab_or_with_another_corpus(tmp_path, capsys):
    def no_lab(lab):
        raise qg.GateError("run this with the lab's Python")

    assert qg.main(["--lab", str(tmp_path)], load=no_lab) == qg.EXIT_ERROR
    assert "QUALITY GATE COULD NOT RUN" in capsys.readouterr().err
    result = run_main(tmp_path, make_records(101), {})
    assert (result.code, result.report, result.predicted) == (qg.EXIT_ERROR, None, False)
    repeated = make_records()
    repeated[-1] = {**repeated[-1], "variable_id": repeated[0]["variable_id"]}
    result = run_main(tmp_path, repeated, {})
    assert (result.code, result.report, result.predicted) == (qg.EXIT_ERROR, None, False)
    assert "repeats a variable id" in capsys.readouterr().err


def test_a_stale_container_is_refused_before_any_model_call(tmp_path, capsys):
    stale = {**RUN, "code_sha256": "0" * 64}
    result = run_main(tmp_path, make_records(), {}, run=stale)
    assert (result.code, result.report, result.predicted) == (qg.EXIT_ERROR, None, False)
    assert "rebuild it with docker compose up -d --build" in capsys.readouterr().err


def test_another_backend_is_refused_unless_asked_for(tmp_path, capsys):
    records = make_records()
    predictions = {r["variable_id"]: answer(r) for r in records}
    openrouter = {**RUN, "provider": "openrouter", "model": "qwen/qwen3-32b", "prompt_setup": "few-shot:v3"}
    result = run_main(tmp_path, records, predictions, run=openrouter)
    assert (result.code, result.report, result.predicted) == (qg.EXIT_ERROR, None, False)
    err = capsys.readouterr().err
    assert "provider is 'openrouter', not 'psnc'" in err and "--any-backend" in err
    sampling = {**RUN, "temperature": 0.7}
    result = run_main(tmp_path, records, predictions, run=sampling)
    assert (result.code, result.predicted) == (qg.EXIT_ERROR, False)
    assert "temperature is 0.7, not 0.5" in capsys.readouterr().err

    result = run_main(tmp_path, records, predictions, "--any-backend", run=openrouter)
    assert (result.code, result.report["run"]["provider"]) == (qg.EXIT_PASS, "openrouter")


def test_a_scorer_that_breaks_after_the_calls_keeps_the_answers(tmp_path, capsys):
    def evaluate(gold, prediction, variable_id):
        raise RuntimeError("the embedding model is missing")

    records = make_records()
    broken = dataclasses.replace(fake_scorer(), evaluate=evaluate)
    result = run_main(tmp_path, records, {r["variable_id"]: answer(r) for r in records}, scorer=broken)
    assert (result.code, result.report["verdict"]) == (qg.EXIT_ERROR, "error")
    assert "embedding model is missing" in result.report["error"]
    assert len(result.report["predictions"]) == 102
    assert "The answers are kept in" in capsys.readouterr().err


def test_the_code_digest_follows_code_and_data_but_not_bytecode(tmp_path):
    for name, text in (("app/main.py", "print(1)\n"), ("data/prompt.txt", "x"), ("tools/other.py", "y")):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    first = container.code_digest(tmp_path)
    (tmp_path / "app/__pycache__").mkdir()
    (tmp_path / "app/__pycache__/main.cpython-312.pyc").write_bytes(b"\0")
    (tmp_path / "app/.DS_Store").write_bytes(b"\0")
    (tmp_path / "tools/other.py").write_text("changed")
    assert container.code_digest(tmp_path) == first
    (tmp_path / "data/prompt.txt").write_text("z")
    assert container.code_digest(tmp_path) != first
    (tmp_path / "data/prompt.txt").write_text("x")
    (tmp_path / "app/new.py").write_text("")
    assert container.code_digest(tmp_path) != first


class FakeProcess:
    def __init__(self, lines, code, broken_pipe=False):
        self.stdin = io.StringIO()
        self.stdin.close = lambda: None  # keep the written text readable for the test
        if broken_pipe:
            def refuse(text):
                raise BrokenPipeError("the container is gone")
            self.stdin.write = refuse
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
    with pytest.raises(qg.GateError, match="exited with 1"):  # a closed pipe is reported, not raised
        qg.run_predictions([{"variable_id": "v000", "definition": "d"}], 4,
                           popen=lambda command, **kwargs: FakeProcess([], 1, broken_pipe=True), cwd=tmp_path)


def test_describe_asks_the_container_without_calling_a_model(tmp_path):
    seen = {}

    def run_command(command, **kwargs):
        seen.update(command=command, input=kwargs["input"])
        return SimpleNamespace(returncode=0, stdout="log line\n" + json.dumps(RUN) + "\n", stderr="")

    assert qg.describe_backend(run_command=run_command, cwd=tmp_path)["model"] == "Qwen3.8-27B"
    assert (seen["command"][4], seen["command"][-1], seen["input"]) == ("backend", "describe", "")
    with pytest.raises(qg.GateError, match="exited with 1"):
        qg.describe_backend(run_command=lambda command, **kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="service backend is not running"), cwd=tmp_path)


def test_the_container_program_reports_the_answer_or_the_error(monkeypatch):
    from app import pipeline
    from app.services.call_log import start_call

    def answered(definition, calls=None):
        start_call(calls, "decomposition", "psnc", "Qwen3.8-27B").finish("ok", usage={"prompt_tokens": 5208,
                                                                                       "completion_tokens": 113})
        return {"raw_llm_output": '{"hasProperty": "count"}'}

    monkeypatch.setattr(pipeline, "run_pipeline", answered)
    line = container.decompose_one({"variable_id": "v1", "definition": "d"})
    assert (line["ok"], line["provider_failed"], line["raw_output"], line["error"]) == (
        True, False, '{"hasProperty": "count"}', None)
    assert (line["decomposition_tries"], line["prompt_tokens"]) == (1, 5208)

    def broken(definition, calls=None):
        raise RuntimeError("the linker crashed")

    monkeypatch.setattr(pipeline, "run_pipeline", broken)
    line = container.decompose_one({"variable_id": "v1", "definition": "d"})
    assert (line["ok"], line["provider_failed"], line["error"]) == (False, False, "RuntimeError: the linker crashed")

    monkeypatch.setattr(pipeline, "run_pipeline", lambda definition, calls=None: {"raw_llm_output": ""})
    line = container.decompose_one({"variable_id": "v1", "definition": "d"})
    assert (line["ok"], line["provider_failed"], line["error"]) == (False, False, "The pipeline returned no answer.")

    def refused(definition, calls=None):
        for _ in range(3):
            start_call(calls, "decomposition", "psnc", "Qwen3.8-27B").finish("error", error="HTTP 401: key refused")
        return {"raw_llm_output": ""}

    monkeypatch.setattr(pipeline, "run_pipeline", refused)
    line = container.decompose_one({"variable_id": "v1", "definition": "d"})
    assert (line["ok"], line["provider_failed"], line["decomposition_tries"]) == (False, True, 3)
    assert line["error"].startswith("The provider never answered: HTTP 401")


def test_the_container_describes_what_it_runs_and_agrees_with_the_gate_on_this_tree(monkeypatch):
    from app import pipeline
    from app.services import measured_prompt

    monkeypatch.setattr(measured_prompt, "load_demonstrations", lambda: [{"definition": "shown one"}])
    monkeypatch.setattr(pipeline, "prompt_setup", lambda provider=None: ("measured", "f" * 64))
    run = container.run_description()
    assert (run["kind"], run["prompt_setup"], run["example_definitions"]) == ("run", "measured", ["shown one"])
    assert {"provider", "model", "temperature", "top_p", "max_tokens", "prompt_sha256"} <= set(run)
    assert run["code_sha256"] == qg.local_code()["sha256"]  # the same tree gives the same digest on both sides
    monkeypatch.setattr(pipeline, "prompt_setup", lambda provider=None: ("few-shot:v3", "f" * 64))
    assert container.run_description()["example_definitions"] is None


def test_the_container_program_writes_the_run_line_first_and_keeps_the_order(monkeypatch):
    monkeypatch.setattr(container, "run_description", lambda: dict(RUN))
    monkeypatch.setattr(container, "decompose_one",
                        lambda item: {"variable_id": item["variable_id"], "ok": True, "raw_output": "{}"})
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps([{"variable_id": f"v{i}", "definition": "d"}
                                                              for i in range(5)])))
    out = io.StringIO()
    container.main("3", out=out)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert lines[0]["kind"] == "run"
    assert [line["variable_id"] for line in lines[1:]] == [f"v{i}" for i in range(5)]

    out = io.StringIO()
    container.main("describe", out=out)
    assert [json.loads(line)["kind"] for line in out.getvalue().splitlines()] == ["run"]


NOISY_PROGRAM = """
import json, runpy, threading
helpers = runpy.run_path(PROGRAM)
out = helpers["protocol_stream"]()

def noisy():
    for _ in range(300):
        print("LLM parse attempt 1 failed: noise")

threads = [threading.Thread(target=noisy) for _ in range(4)]
for thread in threads:
    thread.start()
for number in range(300):
    out.write(json.dumps({"kind": "prediction", "variable_id": number}) + "\\n")
for thread in threads:
    thread.join()
out.flush()
"""


def test_the_protocol_keeps_its_own_output_while_the_app_prints():
    program = NOISY_PROGRAM.replace("PROGRAM", repr(str(TOOLS / "quality_gate_container.py")))
    completed = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    assert [json.loads(line)["variable_id"] for line in completed.stdout.splitlines()] == list(range(300))
    assert completed.stderr.count("noise") == 1200
