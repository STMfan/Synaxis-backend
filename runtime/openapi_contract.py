"""Generate the public API contract without credentials, runtime data or network."""

import argparse
import json
import os
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
CONTRACT = BACKEND / "contracts/openapi.json"
METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
ANNOTATIONS = {"title", "description", "summary", "example", "examples", "externalDocs"}


def current_schema():
    # api.app also exposes an ASGI app on import. Suppress its default dotenv
    # read as well as constructor/environment overrides before that import.
    with patch.dict(os.environ, {"SYNAXIS_CONFIG_DIR": str(BACKEND / "config")}, clear=True):
        from core.settings import Settings

        with patch.dict(Settings.model_config, {"env_file": None}):
            from api.app import create_app
            from api.docs import localized_schema

            settings = Settings(_env_file=None, config_dir=BACKEND / "config")
            return localized_schema(create_app(settings).openapi(), "en")


def encoded(schema):
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _wire(value, document, seen=frozenset(), members=False):
    """Resolve local refs while dropping only documentation annotations."""
    if isinstance(value, list):
        return [_wire(item, document, seen) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        ref = value["$ref"]
        if not ref.startswith("#/"):
            raise ValueError("Only local contract references are supported")
        if ref in seen:
            return {"$ref": ref}
        target = document
        for part in ref[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        resolved = _wire(target, document, seen | {ref})
        siblings = _wire({key: item for key, item in value.items() if key != "$ref"}, document, seen)
        return {"resolved": resolved, **siblings} if siblings else resolved
    return {
        key: deepcopy(item)
        if not members and key in {"default", "const", "enum"}
        else _wire(
            item, document, seen, members=not members and key in {"properties", "patternProperties", "$defs"}
        )
        for key, item in value.items()
        if members or key not in ANNOTATIONS
    }


def compatibility_issues(previous, current):
    """Conservative wire guard, not a proof of all application semantics.

    Old operations/parameters/success media/SSE variants must remain. Changed
    existing schemas (including constraints/defaults) require explicit review;
    additive paths, responses and optional operation parameters are accepted.
    """
    issues = []
    old_schemes = previous.get("components", {}).get("securitySchemes", {})
    new_schemes = current.get("components", {}).get("securitySchemes", {})
    for path, item in previous.get("paths", {}).items():
        for method, old in item.items():
            if method not in METHODS:
                continue
            label = f"{method.upper()} {path}"
            new = current.get("paths", {}).get(path, {}).get(method)
            if new is None:
                issues.append(f"{label}: operation removed")
                continue
            if old.get("security", previous.get("security", [])) != new.get(
                "security", current.get("security", [])
            ):
                issues.append(f"{label}: authentication changed")
            for requirement in old.get("security", previous.get("security", [])):
                for name in requirement:
                    if _wire(old_schemes.get(name), previous) != _wire(new_schemes.get(name), current):
                        issues.append(f"{label}: authentication scheme changed: {name}")
            old_parameters = {
                (p["in"], p["name"]): p for p in [*item.get("parameters", []), *old.get("parameters", [])]
            }
            new_item = current["paths"][path]
            new_parameters = {
                (p["in"], p["name"]): p for p in [*new_item.get("parameters", []), *new.get("parameters", [])]
            }
            for key, parameter in old_parameters.items():
                replacement = new_parameters.get(key)
                if replacement is None or _wire(parameter, previous) != _wire(replacement, current):
                    issues.append(f"{label}: parameter changed: {key[0]} {key[1]}")
            for key, parameter in new_parameters.items():
                if key not in old_parameters and parameter.get("required"):
                    issues.append(f"{label}: new required parameter: {key[0]} {key[1]}")
            if _wire(old.get("requestBody"), previous) != _wire(new.get("requestBody"), current):
                issues.append(f"{label}: request body changed")
            for code, response in old.get("responses", {}).items():
                if not str(code).startswith("2"):
                    continue
                replacement = new.get("responses", {}).get(code)
                if replacement is None:
                    issues.append(f"{label}: success response removed: {code}")
                    continue
                for media, content in response.get("content", {}).items():
                    candidate = replacement.get("content", {}).get(media)
                    if candidate is None or _wire(content, previous) != _wire(candidate, current):
                        issues.append(f"{label}: response schema changed: {code} {media}")
            if _wire(old.get("x-sse-event-schema"), previous) != _wire(
                new.get("x-sse-event-schema"), current
            ):
                issues.append(f"{label}: SSE event contract changed")
    return issues


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate/check the public backend OpenAPI contract")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="Update contracts/openapi.json for review")
    mode.add_argument("--check", action="store_true", help="Require the checked-in contract to match source")
    parser.add_argument("--against", type=Path, help="Also check a prior consumer OpenAPI snapshot")
    args = parser.parse_args(argv)
    try:
        schema = current_schema()
        if args.against:
            issues = compatibility_issues(json.loads(args.against.read_text(encoding="utf-8")), schema)
            if issues:
                for issue in issues[:30]:
                    print(issue)
                print(f"Contract review required: {len(issues)} wire changes")
                return 1
        if args.write:
            CONTRACT.parent.mkdir(parents=True, exist_ok=True)
            CONTRACT.write_text(encoded(schema), encoding="utf-8")
            print("Updated contracts/openapi.json")
        elif not CONTRACT.is_file() or CONTRACT.read_text(encoding="utf-8") != encoded(schema):
            print("Contract differs from source; regenerate with --write and review the diff")
            return 1
        else:
            print("OpenAPI contract matches source")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Contract check failed: {type(exc).__name__}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
