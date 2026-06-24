"""Parity tests: the typed Settings must resolve exactly like the original os.getenv logic.

Phase 2 replaced ~24 module-level ``os.getenv(...)`` reads in ``app.main`` with a
single ``app.core.config.Settings``. These tests reconstruct the original parsing
inline and assert the new Settings produces identical values under a matrix of
environments (defaults, explicit values, and the boolean edge cases where the two
original flags differed).
"""

from __future__ import annotations

import importlib
import os
import pathlib
from typing import Dict

import pytest


def _reference_values(env: Dict[str, str]) -> dict:
    """Compute the configuration values the *original* app.main logic would yield."""

    def getenv(name, default=None):
        return env.get(name, default)

    def env_bool(name, default=False):
        raw = env.get(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    base_dir = pathlib.Path("/app")  # value is path-shaped; we compare suffixes below

    def dedupe(values):
        seen, out = set(), []
        for v in values:
            c = v.strip()
            if not c or c in seen:
                continue
            seen.add(c)
            out.append(c)
        return out

    DEFAULT_MODEL_NAME = "qwen/qwen3.5-flash-02-23"
    DEFAULT_MODEL_NAMES = [
        "qwen/qwen3.5-flash-02-23",
        "qwen/qwen3-32b",
        "qwen/qwen3.5-397b-a17b",
        "google/gemini-3-flash-preview",
    ]
    DEFAULT_PSNC_MODEL_NAME = "Qwen3.5-397B-A17B"
    DEFAULT_PSNC_MODEL_NAMES = ["Qwen3.5-397B-A17B", "Qwen3-VL-235B-A22B-Instruct-FP8"]

    model_name = getenv("MODEL_NAME", DEFAULT_MODEL_NAME)
    configured_models = [v.strip() for v in getenv("MODEL_NAMES", "").split(",") if v.strip()]
    model_names = dedupe([model_name, *(configured_models or DEFAULT_MODEL_NAMES)])
    psnc_model_name = getenv("PSNC_MODEL_NAME", DEFAULT_PSNC_MODEL_NAME)
    configured_psnc = [v.strip() for v in getenv("PSNC_MODEL_NAMES", "").split(",") if v.strip()]
    psnc_model_names = dedupe([psnc_model_name, *(configured_psnc or DEFAULT_PSNC_MODEL_NAMES)])

    return {
        "temperature": float(getenv("TEMPERATURE", "0.5")),
        "openrouter_api_key": getenv("OPENROUTER_API_KEY"),
        "model_name": model_name,
        "model_names": model_names,
        "psnc_model_name": psnc_model_name,
        "psnc_model_names": psnc_model_names,
        "psnc_api_key": getenv("PSNC_API_KEY"),
        "psnc_api_base_url": getenv("PSNC_API_BASE_URL", "https://llm.hpc.psnc.pl"),
        "psnc_rerank_model": getenv("PSNC_RERANK_MODEL", "bge-reranker-v2-m3"),
        "rerank_threshold": float(getenv("RERANK_THRESHOLD", "0.10")),
        "enable_wikidata_linking": getenv("ENABLE_WIKIDATA_LINKING", "true").lower() == "true",
        "nanopub_publish_server": getenv("NANOPUB_PUBLISH_SERVER", "https://registry.petapico.org/np/"),
        "nanopub_pubinfo_template_uris": [
            u.strip()
            for u in getenv(
                "NANOPUB_PUBINFO_TEMPLATE_URIS",
                "https://w3id.org/np/RAA2MfqdBCzmz9yVWjKLXNbyfBNcwsMmOqcNUxkk1maIM,"
                "https://w3id.org/np/RA0J4vUn_dekg-U1kK3AOEt02p9mT2WO03uGxLDec1jLw,"
                "https://w3id.org/np/RAukAcWHRDlkqxk7H2XNSegc1WnHI569INvNr-xdptDGI",
            ).split(",")
            if u.strip()
        ],
        "auth_enabled": env_bool("IADOPT_AUTH_ENABLED", False),
        "cookie_secure": env_bool("IADOPT_COOKIE_SECURE", False),
        "session_secret": getenv("IADOPT_SESSION_SECRET", ""),
        "session_ttl_hours": int(getenv("IADOPT_SESSION_TTL_HOURS", "12")),
        "audit_retention_days": int(getenv("IADOPT_AUDIT_RETENTION_DAYS", "30")),
        "audit_max_payload_bytes": int(getenv("IADOPT_AUDIT_MAX_PAYLOAD_BYTES", "1000000")),
    }


# Each scenario is a full environment dict (only iadopt-relevant keys).
SCENARIOS = {
    "defaults": {},
    "explicit_psnc": {
        "DEFAULT_MODEL_PROVIDER": "psnc",
        "ENABLED_MODEL_PROVIDERS": "psnc",
        "MODEL_NAMES": "a, b ,a,",
        "PSNC_MODEL_NAMES": "Qwen3.5-397B-A17B, X",
        "TEMPERATURE": "0.2",
        "RERANK_THRESHOLD": "0.42",
    },
    "wikidata_flag_one_is_false": {"ENABLE_WIKIDATA_LINKING": "1"},  # "1" != "true" -> False
    "wikidata_flag_true": {"ENABLE_WIKIDATA_LINKING": "TRUE"},
    "auth_bool_variants": {"IADOPT_AUTH_ENABLED": "yes", "IADOPT_COOKIE_SECURE": "on"},
    "auth_bool_false_variants": {"IADOPT_AUTH_ENABLED": "0", "IADOPT_COOKIE_SECURE": "off"},
    "custom_uris": {"NANOPUB_PUBINFO_TEMPLATE_URIS": "https://x/1, https://x/2 ,"},
}

# All iadopt-relevant env keys, so each scenario starts from a clean slate.
_ALL_KEYS = [
    "TEMPERATURE", "OPENROUTER_API_KEY", "MODEL_NAME", "MODEL_NAMES", "PSNC_MODEL_NAME", "PSNC_MODEL_NAMES",
    "ENABLED_MODEL_PROVIDERS", "DEFAULT_MODEL_PROVIDER", "PSNC_API_KEY", "PSNC_API_BASE_URL", "PSNC_RERANK_MODEL",
    "RERANK_THRESHOLD", "ENABLE_WIKIDATA_LINKING", "NANOPUB_PUBLISH_SERVER", "NANOPUB_PUBINFO_TEMPLATE_URIS",
    "IADOPT_AUTH_ENABLED", "IADOPT_COOKIE_SECURE", "IADOPT_SESSION_SECRET", "IADOPT_SESSION_TTL_HOURS",
    "IADOPT_AUDIT_RETENTION_DAYS", "IADOPT_AUDIT_MAX_PAYLOAD_BYTES", "IADOPT_STATE_DIR", "IADOPT_DB_PATH",
]


@pytest.mark.parametrize("name", list(SCENARIOS.keys()))
def test_settings_parity(monkeypatch, name):
    """Each scenario's Settings values equal the reference os.getenv computation."""
    env = SCENARIOS[name]
    for key in _ALL_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    import app.core.config as config_module

    importlib.reload(config_module)
    s = config_module.Settings()

    expected = _reference_values({k: os.environ[k] for k in _ALL_KEYS if k in os.environ})

    for field, exp in expected.items():
        got = getattr(s, field)
        assert got == exp, f"[{name}] {field}: settings={got!r} != reference={exp!r}"


def test_settings_module_reloaded_clean():
    """Reload config a final time with a clean env so the module singleton is sane."""
    import app.core.config as config_module

    importlib.reload(config_module)
    assert config_module.settings.enabled_model_providers  # non-empty
