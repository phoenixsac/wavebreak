"""Provider and model selection of agent/register.py (no network)."""

from __future__ import annotations

import json

import pytest

from agent import register

CATALOG = [
    {"type": "openai", "models": [{"model_id": "gpt-5.5", "name": "gpt-5-5"}]},
    {"type": "google-gemini", "models": []},
]


def test_default_models_per_provider(monkeypatch):
    monkeypatch.delenv("WAVEBREAK_MODEL", raising=False)
    assert register.resolve_model("google-gemini", None) == "google-gemini/gemini-3-6-flash"
    assert register.resolve_model("openai", None) == "openai/gpt-5-5"
    assert register.resolve_model("openai", "openai/gpt-5-6-sol") == "openai/gpt-5-6-sol"
    monkeypatch.setenv("WAVEBREAK_MODEL", "openai/gpt-5-5")
    assert register.resolve_model("openai", None) == "openai/gpt-5-5"
    with pytest.raises(SystemExit):
        register.resolve_model("nope", None)


def test_model_params():
    assert register.model_params("openai/gpt-5-5") == {"reasoning_effort": "medium"}
    assert register.model_params("google-gemini/gemini-3-6-flash") == {
        "temperature": 0.1,
        "reasoning_effort": "medium",
    }
    assert register.model_params("google-gemini/gemini-3-5-flash-lite") == {"temperature": 0.1}


def test_manifest_uses_model_and_gated_tools():
    spec = register.manifest("openai/gpt-5-5")
    assert spec["model"]["name"] == "openai/gpt-5-5" and "temperature" not in spec["model"]["params"]
    fleet = next(s for s in spec["mcp_servers"] if s["name"] == "wavebreak-fleet")
    assert fleet["require_approval_for_tools"] == ["start_wave", "halt_rollout", "rollback"]


def test_read_key_from_env_then_file(tmp_path):
    f = tmp_path / ".env"
    f.write_text("GEMINI_API_KEY=g-file\nOPENAI_API_KEY='o-file'\n")
    assert register.read_key("openai", {"OPENAI_API_KEY": "o-env"}, f) == "o-env"
    assert register.read_key("openai", {}, f) == "o-file"
    assert register.read_key("google-gemini", {}, f) == "g-file"
    assert register.read_key("openai", {}, tmp_path / "missing") is None
    f.write_text("OPENAI_API_KEY=\n")
    assert register.read_key("openai", {}, f) is None


def test_provider_manifest_from_catalog_and_redaction():
    body = register.provider_manifest("openai", "sk-secret", CATALOG)
    m = body["manifest"]
    assert m["type"] == "openai" and m["name"] == "openai" and m["models"] == CATALOG[0]["models"]
    assert m["auth"] == {"api_key": "sk-secret"}
    assert "sk-secret" not in register.redact({"error": "bad key sk-secret"}, "sk-secret")
    assert "sk-secret" not in register.redact(body, "sk-secret")
    with pytest.raises(SystemExit):
        register.provider_manifest("anthropic", "k", CATALOG)
    json.dumps(body)
