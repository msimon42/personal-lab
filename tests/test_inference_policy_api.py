"""Optional inference configuration retains strict application contracts."""
import uuid

import pytest

from backend.app.service import Service


def test_policy_configuration_is_explicit_bounded_and_never_returns_credentials(tmp_path, monkeypatch):
    secret = "sk-or-v1-dummy-test-secret"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    service = Service(tmp_path / "state")
    try:
        bootstrap = service.bootstrap()
        workspace = bootstrap["workspaces"][0]["id"]
        assert bootstrap["inference"]["key_configured"]
        assert secret not in str(bootstrap)

        def configure(payload):
            return service.command({"command": "configure_inference", "workspace_id": workspace,
                                    "payload": payload, "idempotency_key": uuid.uuid4().hex})

        for payload in ({"enabled": True}, {"enabled": False, "api_key": secret},
                        {"budgets": {"max_calls": True}}, {"budgets": {"max_calls": 100000}}):
            with pytest.raises(ValueError):
                configure(payload)
        policy = {"enabled": True, "model": "test/model", "allowed_providers": ["Test provider"],
                  "excluded_patterns": ["private/**"], "source_policy_confirmed": True,
                  "provider_policy_confirmed": True, "tools_supported": True}
        saved = configure(policy)
        assert secret not in str(saved)
        assert service.inference_policy()["model"] == "test/model"
    finally:
        service.close()
