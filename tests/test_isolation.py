"""The default suite runs without private credentials, services or model data."""

import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from conftest import settings_environment_names

from core.settings import Settings


def test_settings_defaults_do_not_read_developer_credentials():
    assert Settings.model_config["env_file"] is None
    assert not ({name.casefold() for name in os.environ} & settings_environment_names())
    value = Settings()
    for name in ("deepseek_key", "siliconflow_key", "text_main_key", "text_process_key", "admin_password_hash"):
        assert not getattr(value, name).get_secret_value()


def test_environment_isolation_covers_modern_and_legacy_aliases():
    assert {
        "text_main_api_key",
        "text_literature_provider",
        "deepseek-apikey",
        "siliconflow-apikey",
        "image-key",
        "jwt_secret",
        "syllabus_data_dir",
        "synaxis_config_dir",
    } <= settings_environment_names()


def test_explicit_synthetic_dotenv_still_supports_configuration_tests(tmp_path, monkeypatch):
    source = tmp_path / ".env"
    source.write_text("PORT=6789\nTEXT_MAIN_API_KEY=synthetic-fixture-key\n", encoding="utf-8")
    value = Settings(_env_file=source)
    assert value.port == 6789
    assert value.text_main_key.get_secret_value() == "synthetic-fixture-key"
    monkeypatch.setenv("PORT", "7890")
    assert Settings(_env_file=source).port == 7890


def test_default_fixture_has_private_data_isolated(settings, tmp_path):
    assert settings.data_dir == tmp_path
    assert settings.syllabus_data_dir == tmp_path / "syllabus/data"
    assert settings.syllabus_model_cache == tmp_path / "syllabus/models"


def test_unit_suite_rejects_unmocked_sync_http_before_dispatch():
    with httpx.Client(trust_env=False) as client, pytest.raises(AssertionError, match="live HTTP is disabled"):
        client.post("https://provider.invalid/v1/messages", json={"synthetic": True})


async def test_unit_suite_rejects_unmocked_async_http_before_dispatch():
    async with httpx.AsyncClient(trust_env=False) as client:
        with pytest.raises(AssertionError, match="live HTTP is disabled"):
            await client.post("https://provider.invalid/v1/messages", json={"synthetic": True})


def test_collection_ignores_host_configuration_before_importing_asgi_app(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    dotenv = tmp_path / ".env"
    dotenv.write_text("PORT=invalid-private-value\nTEXT_MAIN_API_KEY=synthetic-dotenv-key\n", encoding="utf-8")
    environment = os.environ.copy()
    environment.update(
        PORT="invalid-private-port",
        SYNAXIS_CONFIG_DIR=str(tmp_path / "missing-private-config"),
        TEXT_MAIN_API_KEY="synthetic-host-key",
    )
    script = """
import os
import sys
from pathlib import Path
from unittest.mock import patch
from pydantic_settings.sources import DotEnvSettingsSource

backend, dotenv = map(Path, sys.argv[1:])
with patch.dict(os.environ, {"SYNAXIS_CONFIG_DIR": str(backend / "config")}, clear=True):
    from core.settings import Settings
Settings.model_config["env_file"] = dotenv

def forbidden_read(*args, **kwargs):
    raise AssertionError("Test collection attempted to read private dotenv")

with patch.object(DotEnvSettingsSource, "_read_env_file", forbidden_read):
    import pytest
    code = pytest.main(["tests/test_isolation.py", "--collect-only", "-q", "-k", "test_settings_defaults_do_not_read_developer_credentials"])
    assert code == 0
    from api.app import app
    assert app.state.settings.port == 5667
    assert not app.state.settings.text_main_key.get_secret_value()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(backend), str(dotenv)],
        cwd=backend,
        env=environment,
        text=True,
        capture_output=True,
        timeout=45,
    )
    assert result.returncode == 0, (result.stdout + result.stderr)[-4000:]
