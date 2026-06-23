"""
user_auth.py  —  User registration, login, and credential management.

Credentials stored in:  data/users.json
User data stored in:    data/{username}/
"""

import json
import os
import re
import tempfile

import bcrypt

# ── paths ─────────────────────────────────────────────────────────────────────

DATA_ROOT  = "data"
USERS_FILE = os.path.join(DATA_ROOT, "users.json")


# ── helpers ───────────────────────────────────────────────────────────────────

def _read_users() -> dict:
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _write_users(users: dict) -> None:
    os.makedirs(DATA_ROOT, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=DATA_ROOT, delete=False, suffix=".tmp"
    ) as tmp:
        json.dump(users, tmp, indent=2)
        tmp_path = tmp.name
    os.replace(tmp_path, USERS_FILE)


def _validate_username(username: str) -> str | None:
    """Return an error string if invalid, else None."""
    if not username:
        return "Username cannot be empty."
    if not re.fullmatch(r"[a-zA-Z0-9_]{3,32}", username):
        return "Username must be 3–32 characters: letters, digits, or underscores only."
    return None


def _validate_password(password: str) -> str | None:
    if len(password) < 6:
        return "Password must be at least 6 characters."
    return None


# ── public API ────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False


def get_user_data_dir(username: str) -> str:
    """Return (and create) the per-user data directory."""
    path = os.path.join(DATA_ROOT, username)
    os.makedirs(os.path.join(path, "memory"), exist_ok=True)
    return path


def list_users() -> list[str]:
    return list(_read_users().keys())


def register_user(username: str, password: str) -> tuple[bool, str]:
    """
    Register a new user.

    Returns:
        (True, "")           on success
        (False, error_msg)   on failure
    """
    err = _validate_username(username)
    if err:
        return False, err

    err = _validate_password(password)
    if err:
        return False, err

    users = _read_users()
    if username in users:
        return False, f"Username '{username}' is already taken."

    users[username] = {"password_hash": hash_password(password)}
    _write_users(users)
    get_user_data_dir(username)          # create directory tree
    return True, ""


def login_user(username: str, password: str) -> tuple[bool, str]:
    """
    Validate credentials.

    Returns:
        (True, "")           on success
        (False, error_msg)   on failure
    """
    users = _read_users()
    if username not in users:
        return False, "Invalid username or password."

    stored_hash = users[username].get("password_hash", "")
    if not verify_password(password, stored_hash):
        return False, "Invalid username or password."

    return True, ""