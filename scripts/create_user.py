"""Create a staff account from the command line.

    python -m scripts.create_user --username admin --name "Ward Admin" --role admin

Used to bootstrap the first administrator, who then adds everyone else in the UI.
The password is read from a prompt (or WARD_INITIAL_PASSWORD) so it never lands
in shell history.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from app import auth
from app.db import SessionLocal, init_db
from app.models import Role


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a ward staff account.")
    parser.add_argument("--username", required=True)
    parser.add_argument("--name", required=True, help="Full name, shown on audit lines.")
    parser.add_argument("--role", required=True, choices=[r.value for r in Role])
    parser.add_argument("--api-token", action="store_true", help="Also issue a bearer token.")
    args = parser.parse_args()

    password = os.environ.get("WARD_INITIAL_PASSWORD") or getpass.getpass("Password: ")
    if len(password) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        return 1

    init_db()
    db = SessionLocal()
    try:
        user, token = auth.create_user(
            db, username=args.username, full_name=args.name,
            password=password, role=Role(args.role), with_token=args.api_token,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()

    print(f"Created {user.username} ({user.role.value}).")
    if token:
        print(f"API token (shown once): {token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
