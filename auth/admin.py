"""Local-only database administration; intentionally no registration HTTP endpoint."""

import argparse
import getpass
import sqlite3

from auth.security import hash_password
from core.settings import Settings
from storage.database import Database, now


def main():
    parser = argparse.ArgumentParser(description="Synaxis users table maintenance / 本地用户表维护")
    parser.add_argument(
        "command", choices=["init", "hash-password", "add-user", "disable-user", "revoke-tokens"]
    )
    parser.add_argument("username", nargs="?")
    args = parser.parse_args()
    settings = Settings()
    db = Database(settings.db_path)
    db.initialize()
    if args.command == "init":
        print(settings.db_path)
        return
    if args.command == "hash-password":
        print(hash_password(getpass.getpass("Password (12+ characters): ")))
        return
    if not args.username or len(args.username) > 100:
        parser.error("A username with at most 100 characters is required")
    if args.command == "add-user":
        password = getpass.getpass("Password (12+ characters): ")
        if password != getpass.getpass("Confirm password: "):
            parser.error("Passwords do not match")
        try:
            with db.connection() as conn:
                conn.execute(
                    "INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                    (args.username, hash_password(password), now()),
                )
        except (ValueError, sqlite3.IntegrityError) as exc:
            parser.error(str(exc))
    else:
        with db.connection() as conn:
            if args.command == "disable-user":
                cursor = conn.execute(
                    "UPDATE users SET active=0,token_version=token_version+1 WHERE username=?",
                    (args.username,),
                )
            else:
                cursor = conn.execute(
                    "UPDATE users SET token_version=token_version+1 WHERE username=?", (args.username,)
                )
        if not cursor.rowcount:
            parser.error("User not found")
    print("Updated users table / 用户表已更新")


if __name__ == "__main__":
    main()
