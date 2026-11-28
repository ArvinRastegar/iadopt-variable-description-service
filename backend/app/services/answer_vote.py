"""The most typical of three answers: the lab's check of an answer and the scorer's Exact F1 between answers.

The lab's best pipeline sends the fill prompt three times side by side and keeps the answer that agrees most with the
other two (``few-shot-selection/guided/fill_levers_test.py``, arm ``best+typical3``: +0.009 Close F1 on PSNC, with
fewer unusable answers). There, two answers agree by the frozen scorer's Close F1 of one scored against the other,
averaged over both ways, which needs the scorer's local similarity model. The web app uses the scorer's Exact F1
instead, which needs none: on the lab's stored PSNC answers it keeps the same answer in 278 of 306 groups of three,
for +0.008 (95% +0.0005 to +0.016) against the lab's +0.009 (and +0.0131 against +0.0130 in groups of five).

- :func:`lab_answer` reads an answer as the lab's scoring did (``lab_json``, the lexical schema, the lab's semantic
  checks) and returns the object it holds, as the lab scored it, or ``None`` when the lab could not use it.
- :func:`exact_f1` is the scorer's Exact mode (``iadopt_eval.core``, scorer ``january-derived-member-credit-v1``,
  threshold 1.0), with its exact fractions; :func:`agreement` averages it over both ways.
- :func:`pick` keeps the answer with the highest mean agreement with the others. Ties go to the first answer the lab
  could use, else to the first: when at most one answer is usable every mean is 0, and the lab's rule (ties to the
  first) would keep an unusable first answer over a usable one. In the lab's 306 stored groups that never happened,
  so the two rules keep the same answers there.

Depends on ``jsonschema``, the measured lexical schema and ``lab_json``.
"""

from __future__ import annotations

import json
import re
import unicodedata
from fractions import Fraction
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from jsonschema import Draft202012Validator

from ..core.state import app_state
from .lab_json import extract_json_object
from .measured_prompt import load_lexical_schema_text

ANSWERS = 3
"""How many fill answers a decomposition asks for on the measured path (the lab's best pipeline)."""

VOTE_RULE = ("most typical of the answers by the scorer's Exact F1, both ways; ties to the first the lab's check "
             "accepts, else the first")

_ENTITY_FIELDS = ("hasObjectOfInterest", "hasMatrix", "hasContextObject")
_FIELDS = ("hasStatisticalModifier", "hasProperty", *_ENTITY_FIELDS, "hasConstraint")
_VALIDATORS: Dict[str, Draft202012Validator] = {}


def _lexical_key(text: str) -> str:
    """The lab's cross-reference key: NFC, stripped, case-folded."""
    return unicodedata.normalize("NFC", text).strip().casefold()


def _lexical_order(text: str) -> Tuple[str, str]:
    return unicodedata.normalize("NFC", text).casefold(), text


def _display_label(system: Dict[str, Any]) -> str:
    """The lab's display label of a system: its parts, a ratio or a source and target."""
    if "SymmetricSystem" in system:
        return " + ".join(sorted(system["hasPart"], key=_lexical_order))
    if "hasNumerator" in system:
        return system["hasNumerator"] + " / " + system["hasDenominator"]
    return system["hasSource"] + " → " + system["hasTarget"]


def _pointer(parts: Iterable[Any]) -> str:
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def _targets(candidate: Dict[str, Any]) -> Dict[str, set]:
    """Every value a constraint may name, by the lab's key: as the lab's ``_targets`` indexes them."""
    result: Dict[str, set] = {}
    for field in _FIELDS[:-1]:
        value = candidate[field]
        if isinstance(value, str):
            entries = [(value, value)] if value.strip() else []
        else:
            display = _display_label(value)
            container = "SymmetricSystem" if "SymmetricSystem" in value else "AsymmetricSystem"
            entries = [(display, display)]
            if value[container].strip():
                entries.append((value[container], display))
            if container == "SymmetricSystem":
                entries.extend((part, part) for part in value["hasPart"])
            else:
                entries.extend((part, part) for role, part in value.items() if role != container)
        for alias, resolved in entries:
            result.setdefault(_lexical_key(alias), set()).add(resolved)
    return result


def _lexical_validator() -> Draft202012Validator:
    """The lexical schema the prompt shows, compiled once."""
    cached = app_state.measured_assets_cache
    text = cached[1] if cached is not None else load_lexical_schema_text()
    validator = _VALIDATORS.get(text)
    if validator is None:
        schema = json.loads(text)
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        _VALIDATORS.clear()
        _VALIDATORS[text] = validator
    return validator


def lexical_errors(candidate: Any) -> List[Dict[str, Any]]:
    """The lab's ``validate_prediction`` errors for an answer's own object, in the lab's order.

    Each error has ``stage``, ``code``, ``pointer`` and ``message``: the lexical schema's errors, and when there are
    none, values of the five text fields that are all whitespace, symmetric systems with two equal parts, and
    constraints whose ``on`` names no value, or more than one, of the answer.
    """
    try:
        json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        return [{"stage": "schema", "code": "not_json_value", "pointer": "",
                 "message": "Candidate is not a finite UTF-8 JSON value."}]
    errors: List[Dict[str, Any]] = [
        {"stage": "schema", "code": "schema_" + str(error.validator), "pointer": _pointer(error.absolute_path),
         "message": error.message}
        for error in _lexical_validator().iter_errors(candidate)
    ]
    if errors:
        errors.sort(key=lambda item: (item["pointer"], item["code"], item["message"]))
        return errors
    for field in ("hasStatisticalModifier", "hasProperty", *_ENTITY_FIELDS):
        value = candidate[field]
        if isinstance(value, str) and value and not value.strip():
            errors.append({"stage": "semantic", "code": "whitespace_only_text", "pointer": "/" + field,
                           "message": "A value must be empty or contain non-whitespace."})
    for field in _ENTITY_FIELDS:
        value = candidate[field]
        if isinstance(value, dict) and "hasPart" in value:
            keys = [_lexical_key(part) for part in value["hasPart"]]
            if len(set(keys)) != len(keys):
                errors.append({"stage": "semantic", "code": "duplicate_system_part", "pointer": "/" + field + "/hasPart",
                               "message": "Symmetric members must be unique under lexical target normalization."})
    targets = _targets(candidate)
    for index, constraint in enumerate(candidate["hasConstraint"]):
        matches = targets.get(_lexical_key(constraint["on"]), set())
        if len(matches) != 1:
            code = "unknown_constraint_target" if not matches else "ambiguous_constraint_target"
            errors.append({"stage": "semantic", "code": code, "pointer": f"/hasConstraint/{index}/on",
                           "message": "Constraint target must resolve to exactly one emitted lexical value."})
    errors.sort(key=lambda item: (item["pointer"], item["code"], item["message"]))
    return errors


def lab_answer(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """An answer as the lab scored it: the object it holds when the lab's check accepts it, else ``None``.

    The lab's scoring (``engine.score``) scored the extracted object itself, not a canonical form of it.
    """
    candidate = extract_json_object(text or "")
    if candidate is None or lexical_errors(candidate):
        return None
    return candidate


# --- the scorer's Exact mode (iadopt_eval.core, threshold 1.0) ------------------------------------------------------ #

_COMPONENTS = ("hasStatisticalModifier", "hasProperty", "hasObjectOfInterest", "hasMatrix", "hasContextObject",
               "hasConstraint")
_ON_PREFIX = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*:\s*(.+)$")


def _equal(gold: str, prediction: str) -> bool:
    """The scorer's scalar match in Exact mode: both non-empty and equal once lower-cased and stripped."""
    return bool(gold) and bool(prediction) and gold.lower().strip() == prediction.lower().strip()


def _members(value: Any) -> Tuple[str, List[str]]:
    """An entity's representation and members, as the scorer expands it."""
    if isinstance(value, str):
        return "simple", [value]
    if "SymmetricSystem" in value:
        return "symmetric", sorted(value["hasPart"], key=lambda part: part.encode("utf-8"))
    roles = ("hasNumerator", "hasDenominator") if "hasNumerator" in value else ("hasSource", "hasTarget")
    return "asymmetric", [value[role] for role in roles]


def _system(gold: Any, prediction: Any) -> Tuple[Fraction, Fraction, Fraction, Fraction]:
    """D-022's member credit: the largest matching of equal members, in Exact mode."""
    gold_type, gold_members = _members(gold)
    prediction_type, prediction_members = _members(prediction)
    if gold_type == prediction_type == "asymmetric":
        matched = sum(_equal(g, p) for g, p in zip(gold_members, prediction_members))
    else:
        literal = gold_type == prediction_type == "symmetric"
        key = (lambda label: label) if literal else (lambda label: label.lower().strip())
        usable_gold = [key(g) for g in gold_members if literal or g]
        usable_prediction = [key(p) for p in prediction_members if literal or p]
        left = list(usable_prediction)
        matched = 0
        for label in usable_gold:
            if label in left:
                left.remove(label)
                matched += 1
    g, p = len(gold_members), len(prediction_members)
    union = g + p - matched
    return Fraction(matched, union), Fraction(p - matched, union), Fraction(g - matched, union), Fraction(0)


def _normalize_constraint(constraint: Mapping[str, str]) -> Dict[str, str]:
    target = constraint["on"]
    match = _ON_PREFIX.match(target)
    if match and match.group(1) in _COMPONENTS:
        target = match.group(2).strip()
    return {"label": re.sub(r"\s+", " ", constraint["label"].strip().lower()),
            "on": re.sub(r"\s+", " ", target.strip().lower())}


def _constraints(gold: Sequence[Mapping[str, str]], prediction: Sequence[Mapping[str, str]]) -> Tuple[Fraction, ...]:
    """The January greedy constraint branch, with its floating-point arithmetic, in Exact mode."""
    if not gold:
        return Fraction(0), Fraction(int(bool(prediction))), Fraction(0), Fraction(int(not prediction))
    gold_normalized = [_normalize_constraint(item) for item in gold]
    prediction_normalized = [_normalize_constraint(item) for item in prediction]
    unit = 1.0 / (2 * len(gold))
    candidates = []
    for i, gold_item in enumerate(gold_normalized):
        for j, prediction_item in enumerate(prediction_normalized):
            label = 1.0 if _equal(gold_item["label"], prediction_item["label"]) else 0.0
            target = 1.0 if _equal(gold_item["on"], prediction_item["on"]) else 0.0
            candidates.append((i, j, label, target, (label + target) / 2.0))
    tp = fp = fn = 0.0
    used_gold: set = set()
    used_prediction: set = set()
    while True:
        available = [c for c in candidates if c[0] not in used_gold and c[1] not in used_prediction]
        if not available:
            break
        chosen = max(available, key=lambda c: c[4])
        used_gold.add(chosen[0])
        used_prediction.add(chosen[1])
        for score in (chosen[2], chosen[3]):
            if score >= 1.0:
                tp += unit
            else:
                fp += unit
    fn += (len(gold) - len(used_gold)) * 2 * unit
    fp += (len(prediction) - len(used_prediction)) * 2 * unit
    total = tp + fp + fn
    if 1.0 - total > 1e-6:
        fp += 1.0 - total
    elif total - 1.0 > 1e-6:
        tp /= total
        fp /= total
        fn /= total
    return Fraction(tp), Fraction(fp), Fraction(fn), Fraction(0)


def _component(gold: Any, prediction: Any, key: str) -> Tuple[Fraction, ...]:
    if key == "hasConstraint":
        return _constraints(gold, prediction)
    if not gold or not prediction:
        counts = (0, 1, 0, 0) if prediction else (0, 0, 1, 0) if gold else (0, 0, 0, 1)
        return tuple(Fraction(c) for c in counts)
    if key in _ENTITY_FIELDS and (isinstance(gold, Mapping) or isinstance(prediction, Mapping)):
        return _system(gold, prediction)
    hit = _equal(gold, prediction)
    return Fraction(int(hit)), Fraction(int(not hit)), Fraction(0), Fraction(0)


def exact_f1(gold: Mapping[str, Any], prediction: Mapping[str, Any]) -> float:
    """The scorer's Exact F1 of one canonical answer scored against another as if it were the gold."""
    tp = fp = fn = Fraction(0)
    for key in _COMPONENTS:
        counts = _component(gold[key], prediction[key], key)
        tp, fp, fn = tp + counts[0], fp + counts[1], fn + counts[2]
    return float(2 * tp / (2 * tp + fp + fn)) if tp else 0.0


def agreement(first: Optional[Mapping[str, Any]], second: Optional[Mapping[str, Any]]) -> float:
    """How much two answers the lab could use agree: their Exact F1 averaged over both ways; 0 when either is not."""
    if first is None or second is None:
        return 0.0
    return (exact_f1(first, second) + exact_f1(second, first)) / 2


def pick(answers: Sequence[Optional[Mapping[str, Any]]]) -> int:
    """The position of the most typical answer: the highest mean agreement with the others.

    Ties go to the first answer the lab could use (not ``None``), else to the first answer.
    """
    count = len(answers)
    if count < 2:
        return 0
    table = {(a, b): agreement(answers[a], answers[b]) for a in range(count) for b in range(a + 1, count)}

    def mean(position: int) -> float:
        others = [o for o in range(count) if o != position]
        return sum(table[(min(position, o), max(position, o))] for o in others) / len(others)

    return max(range(count), key=lambda position: (mean(position), answers[position] is not None, -position))
