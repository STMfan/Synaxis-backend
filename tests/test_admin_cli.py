import sys

import pytest

from auth import admin
from auth.security import verify_password
from storage.database import Database


def invoke(settings, monkeypatch, args, passwords=()):
    monkeypatch.setattr(admin, "Settings", lambda: settings)
    monkeypatch.setattr(
        sys, "argv", ["auth.admin", *args]
    )
    answers = iter(passwords)
    monkeypatch.setattr(admin.getpass, "getpass", lambda _: next(answers))
    admin.main()


def test_offline_admin_create_disable_revoke(settings, monkeypatch):
    invoke(settings, monkeypatch, ["init"])
    db = Database(settings.db_path)
    assert settings.db_path.exists()
    invoke(settings, monkeypatch, ["add-user", "synthetic"], ["synthetic-password"] * 2)
    row = db.user(username="synthetic")
    assert verify_password(row["password_hash"], "synthetic-password")
    invoke(settings, monkeypatch, ["revoke-tokens", "synthetic"])
    assert db.user(username="synthetic")["token_version"] == 1
    invoke(settings, monkeypatch, ["disable-user", "synthetic"])
    row = db.user(username="synthetic")
    assert row["active"] == 0 and row["token_version"] == 2


def test_offline_hash_password_and_invalid_inputs(settings, monkeypatch, capsys):
    invoke(settings, monkeypatch, ["hash-password"], ["synthetic-password"])
    assert verify_password(capsys.readouterr().out.strip(), "synthetic-password")
    for args, passwords in [
        (["add-user"], ()),
        (["add-user", "x" * 101], ()),
        (["add-user", "alice"], ("synthetic-password", "different-password")),
        (["disable-user", "missing"], ()),
    ]:
        with pytest.raises(SystemExit) as error:
            invoke(settings, monkeypatch, args, passwords)
        assert error.value.code == 2


def test_offline_duplicate_user_is_rejected(settings, monkeypatch):
    invoke(settings, monkeypatch, ["add-user", "alice"], ["synthetic-password"] * 2)
    with pytest.raises(SystemExit):
        invoke(settings, monkeypatch, ["add-user", "alice"], ["synthetic-password"] * 2)
