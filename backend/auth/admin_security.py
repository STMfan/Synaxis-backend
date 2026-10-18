"""Single administrator password, separate from student identities and JWTs."""

import getpass
from pathlib import Path

from auth.security import hash_password


def password_hash(settings):
    override = settings.admin_password_hash.get_secret_value()
    if override:
        return override
    path = settings.data_dir / "admin-password.hash"
    return path.read_text(encoding="utf-8").strip() if path.is_file() else ""


def set_password(settings, prompt=None):
    if settings.admin_password_hash.get_secret_value():
        raise ValueError("ADMIN_PASSWORD_HASH overrides the local file; remove or update that environment override first")
    prompt = prompt or getpass.getpass
    first = prompt("Administrator password / 管理员密码（至少 12 位）: ")
    second = prompt("Repeat password / 再输入一次: ")
    if first != second:
        raise ValueError("Passwords differ / 两次密码不一致")
    encoded = hash_password(first)
    path = Path(settings.data_dir) / "admin-password.hash"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".tmp").open("w", encoding="utf-8") as stream:
        stream.write(encoded + "\n")
    path.with_suffix(".tmp").chmod(0o600)
    path.with_suffix(".tmp").replace(path)
    return path
