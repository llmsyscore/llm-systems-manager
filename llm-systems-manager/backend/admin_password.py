"""Host-side admin password tool for the LLM Systems Manager.

    admin_password.py status
    admin_password.py reset [--user NAME] [--yes] [--porcelain]

reset gives each admin that needs one (or --user) a temporary password, printed once.
Run it with the manager's own Python, as root or as the owner of the data directory.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import auth  # noqa: E402  # sibling
import manager_users  # noqa: E402  # sibling

REPO_ROOT = Path(__file__).resolve().parents[2]


def users_file() -> Path:
    return REPO_ROOT / "data" / "manager_users.json"


def _toml_auth() -> dict:
    """The [manager.auth] table of the live TOML, {} when it cannot be read."""
    cfg = Path(os.environ.get("LLM_SYSTEMS_CONFIG") or REPO_ROOT / "config" / "llm-systems.toml")
    try:
        with open(cfg, "rb") as fh:
            return (tomllib.load(fh).get("manager") or {}).get("auth") or {}
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def configured_user() -> str:
    """[manager.auth].username from the live TOML, else the default admin name."""
    return str(_toml_auth().get("username") or "").strip() or auth.DEFAULT_AUTH_USER


def _provisioned() -> bool:
    """True when the TOML or the legacy data/manager_auth.json carries a password hash to seed."""
    if str(_toml_auth().get("password_hash") or "").strip():
        return True
    try:
        return bool(json.loads((users_file().parent / "manager_auth.json").read_text()).get("password_hash"))
    except (OSError, ValueError, AttributeError):
        return False


def status(store: manager_users.UserStore) -> str:
    if store.is_empty():
        return "ok" if _provisioned() else "empty"
    if store.awaiting_reset(auth.uses_retired_password) or store.needs_reset():
        return "reset-required"
    return "ok"


def reset_targets(store: manager_users.UserStore, user: "str | None") -> "list[str]":
    """--user when given, else every admin that needs a reset, else the configured admin."""
    if user:
        return [user]
    return store.awaiting_reset(auth.uses_retired_password) or [configured_user()]


def reset(store: manager_users.UserStore, user: str) -> "tuple[str, str]":
    """Sets a temporary password on `user`; returns (stored name, password)."""
    password = secrets.token_urlsafe(12)
    name = store.set_temporary(user, auth.scrypt_hash(password))
    path = users_file()
    if os.geteuid() == 0:
        owner = path.parent.stat()
        os.chown(path, owner.st_uid, owner.st_gid)
    return name, password


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(prog="admin_password.py", description="Check or reset the manager admin password.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="print ok, empty or reset-required")
    rp = sub.add_parser("reset", help="give the admin a temporary password")
    rp.add_argument("--user", help="account to reset (default: the configured admin)")
    rp.add_argument("--yes", action="store_true", help="do not ask before replacing a password")
    rp.add_argument("--porcelain", action="store_true", help="print only: <user> <password>")
    args = ap.parse_args(argv)

    path = users_file()
    store = manager_users.UserStore(path)
    if args.cmd == "status":
        print(status(store))
        return 0

    users = reset_targets(store, args.user)
    for user in users:
        if not store.valid_name(store.normalize(user)):
            print(f"invalid user name: {user}", file=sys.stderr)
            return 2
    if not path.parent.is_dir():
        print(f"{path.parent} does not exist; start the manager once, then run this again", file=sys.stderr)
        return 2
    if not args.user and store.get(users[0]) is None and not store.needs_reset():
        admins = ", ".join(u["username"] for u in store.list() if u.get("role") == "admin")
        print(f"no account named '{users[0]}'; name the one to reset with --user (admins: {admins})", file=sys.stderr)
        return 2
    if not args.yes:
        if not sys.stdin.isatty():
            print("refusing to reset without --yes when not run from a terminal", file=sys.stderr)
            return 2
        for user in users:
            exists = store.get(user) is not None
            print(f"This {'replaces the password of' if exists else 'creates the admin account'} "
                  f"'{store.normalize(user)}' with a temporary password.")
        print("A temporary password is shown once and must be changed at the next sign-in.")
        if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing changed.")
            return 1
    for user in users:
        try:
            name, password = reset(store, user)
        except PermissionError:
            print(f"cannot write {path}: run this as root or as the owner of {path.parent}", file=sys.stderr)
            return 2
        if args.porcelain:
            print(name, password)
        else:
            print(f"\n  Sign in as:           {name}\n  Temporary password:   {password}")
    if not args.porcelain:
        print("\nSign in with it now; the dashboard asks for a new password right away.")
        print("If sign-in reports too many attempts, wait for the lockout to pass or restart the manager.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
