"""Regenerate the golden TTL + validation fixtures (clock/RNG/ORCID frozen).

Run inside the backend container:
    docker compose exec -T -w /app -e PYTHONPATH=/app backend python tests/contract/generate_goldens.py > /tmp/goldens.json
then split into golden/*.{input.json,expected.ttl,validation.json}. See README.md.
"""
import json, sys, types
import app.main as m

# Freeze the two nondeterminism sources in TTL generation.
class _FrozenDT:
    @staticmethod
    def now(tz=None):
        from datetime import datetime, timezone
        return datetime(2026,1,2,3,4,5,123456, tzinfo=timezone.utc)
import datetime as _dt
m.datetime = types.SimpleNamespace(now=_FrozenDT.now, timezone=_dt.timezone)
m.random.randint = lambda a,b: 7
# Avoid live ORCID HTTP: force a deterministic creator name resolution.
m._lookup_orcid_display_name = lambda orcid: "Test Creator"

samples = {
  "simple_air_temperature": {
    "label":"maximum air temperature","definition":"Maximum daily air temperature at 2 meters",
    "comment":"max air temp","hasProperty":"temperature","hasStatisticalModifier":"maximum",
    "hasObjectOfInterest":"air","hasConstraint":[{"label":"at 2 meters","on":"air"}]
  },
  "asymmetric_soil_moisture": {
    "label":"Moisture in upper soil portion","definition":"Moisture in upper soil portion measured in kg m-2",
    "comment":"Moisture in upper soil portion measured in kg m-2","hasProperty":"surface density",
    "hasObjectOfInterest":{"AsymmetricSystem":"system","hasSource":"water","hasTarget":"soil"},
    "hasConstraint":[{"label":"layer: upper soil","on":"soil"}]
  },
}

out = {}
for name, pred in samples.items():
    pred = m.coerce_prediction(dict(pred))
    pred["definition"] = pred.get("definition","")
    errs = m.get_schema_validation_errors(pred, label_for_logs=pred.get("label"))
    errs += m._get_constraint_semantic_validation_errors(pred)
    ttl = m.json_to_ttl_repo_style(pred, creator_orcid_id="0000-0003-2195-3997")
    out[name] = {"input": pred, "schema_valid": len(errs)==0, "validation_errors": errs, "ttl": ttl}

# Determinism self-check: regenerate and compare.
for name, pred in samples.items():
    pred2 = m.coerce_prediction(dict(pred)); pred2["definition"]=pred2.get("definition","")
    ttl2 = m.json_to_ttl_repo_style(pred2, creator_orcid_id="0000-0003-2195-3997")
    assert ttl2 == out[name]["ttl"], f"NONDETERMINISTIC: {name}"
print(json.dumps(out, ensure_ascii=False, indent=2))
sys.stderr.write("DETERMINISM_OK: regeneration matched for all samples\n")
