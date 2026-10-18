import copy
import json
import tomllib
from pathlib import Path

from packaging.version import Version

from core.version import DISTRIBUTION_VERSION, VERSION
from runtime import openapi_contract


def fixture_contract():
    return {
        "paths": {
            "/example": {
                "get": {
                    "parameters": [
                        {
                            "in": "query",
                            "name": "after",
                            "required": False,
                            "schema": {"type": "integer", "minimum": 0},
                        }
                    ],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {"schema": {"$ref": "#/components/schemas/Result"}}
                            }
                        }
                    },
                    "x-sse-event-schema": {"type": "string", "enum": ["done"]},
                }
            }
        },
        "components": {
            "schemas": {
                "Result": {
                    "type": "object",
                    "required": ["title"],
                    "properties": {"title": {"type": "string"}},
                }
            }
        },
    }


def test_stable_version_license_and_locked_metadata():
    root = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    assert VERSION == "1.0.0"
    assert not Version(VERSION).is_prerelease
    assert str(Version(VERSION)) == DISTRIBUTION_VERSION == metadata["version"]
    assert next(p["version"] for p in lock["package"] if p["name"] == "synaxis") == DISTRIBUTION_VERSION
    assert metadata["license"] == "MIT" and "LICENSE" in metadata["license-files"]
    assert (root / "LICENSE").is_file() and (root / "NOTICE").is_file()
    indexes = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["uv"]["index"]
    assert indexes == [{"name": "pypi", "url": "https://pypi.org/simple", "default": True}]
    assert {p["source"]["registry"] for p in lock["package"] if "registry" in p["source"]} == {"https://pypi.org/simple"}


def test_contract_is_generated_from_current_source_without_private_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNAXIS_CONFIG_DIR", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("TEXT_MAIN_API_KEY", "synthetic-private-value")
    schema = openapi_contract.current_schema()
    assert schema["info"]["version"] == VERSION
    assert "synthetic-private-value" not in json.dumps(schema)
    assert "SyllabusResult" in schema["components"]["schemas"]
    assert schema["paths"]["/v1/sessions/{session_id}/events"]["get"]["x-sse-event-schema"]
    assert openapi_contract.CONTRACT.read_text(encoding="utf-8") == openapi_contract.encoded(schema)


def test_wire_guard_allows_new_paths_and_documentation_changes():
    old = fixture_contract()
    new = copy.deepcopy(old)
    new["paths"]["/added"] = {"get": {}}
    new["components"]["schemas"]["Result"]["description"] = "Revised documentation"
    assert openapi_contract.compatibility_issues(old, new) == []


def test_wire_guard_detects_response_property_named_title_and_nested_constraint_change():
    old = fixture_contract()
    new = copy.deepcopy(old)
    new["components"]["schemas"]["Result"]["properties"]["title"]["maxLength"] = 10
    assert "response schema changed" in "\n".join(openapi_contract.compatibility_issues(old, new))
    del new["components"]["schemas"]["Result"]["properties"]["title"]
    assert openapi_contract.compatibility_issues(old, new)


def test_wire_guard_detects_removed_operation_required_input_and_sse_variant():
    old = fixture_contract()
    assert "operation removed" in openapi_contract.compatibility_issues(old, {"paths": {}})[0]
    new = copy.deepcopy(old)
    new["paths"]["/example"]["get"]["parameters"].append({"in": "header", "name": "X-New", "required": True})
    new["paths"]["/example"]["get"]["x-sse-event-schema"]["enum"].append("new-output")
    issues = "\n".join(openapi_contract.compatibility_issues(old, new))
    assert "new required parameter" in issues and "SSE event contract changed" in issues


def test_wire_guard_preserves_object_literals_and_authentication_scheme_definitions():
    old = fixture_contract()
    old["components"]["schemas"]["Result"]["default"] = {"title": "original", "description": "literal data"}
    new = copy.deepcopy(old)
    new["components"]["schemas"]["Result"]["default"]["title"] = "changed"
    assert openapi_contract.compatibility_issues(old, new)
    old["security"] = [{"Bearer": []}]
    old["components"]["securitySchemes"] = {"Bearer": {"type": "http", "scheme": "bearer"}}
    new = copy.deepcopy(old)
    new["components"]["securitySchemes"]["Bearer"] = {"type": "apiKey", "in": "header", "name": "X-Key"}
    assert "authentication scheme changed" in "\n".join(openapi_contract.compatibility_issues(old, new))


def test_contract_cli_write_check_drift_and_prior_snapshot(tmp_path, monkeypatch):
    contract = tmp_path / "contract.json"
    monkeypatch.setattr(openapi_contract, "CONTRACT", contract)
    schema = fixture_contract()
    monkeypatch.setattr(openapi_contract, "current_schema", lambda: schema)
    assert openapi_contract.main(["--check"]) == 1
    assert openapi_contract.main(["--write"]) == 0
    assert openapi_contract.main(["--check", "--against", str(contract)]) == 0
    schema["paths"].clear()
    assert openapi_contract.main(["--against", str(contract)]) == 1
    assert openapi_contract.main(["--check"]) == 1
