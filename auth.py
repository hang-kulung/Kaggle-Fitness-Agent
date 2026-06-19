import hashlib
import json
import os
import re
import secrets
import tempfile
import threading

from storage_config import get_data_dir


def _users_file() -> str:
    return os.path.join(get_data_dir(), "users.json")


DATA_DIR = get_data_dir()
USERS_FILE = _users_file()
USERNAME_RE = re.compile(r"^[a-z0-9_-]{3,32}$")
PBKDF2_ITERATIONS = 260_000

_users_lock = threading.RLock()


def _normalize_username(username: str) -> str:
    return username.strip().lower()


def _validate_username(username: str) -> tuple[bool, str]:
    if not username:
        return False, "Username cannot be empty."
    if not USERNAME_RE.fullmatch(username):
        return (
            False,
            "Username must be 3-32 characters and use only lowercase letters, "
            "numbers, underscores, or hyphens.",
        )
    return True, ""


def _legacy_hash_password(password: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}{password}".encode()).hexdigest()


def _hash_password(password: str, salt: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        salt.encode(),
        iterations,
    )
    return digest.hex()


def _new_password_record(password: str) -> dict:
    salt = secrets.token_hex(16)
    return {
        "algorithm": "pbkdf2_sha256",
        "iterations": PBKDF2_ITERATIONS,
        "salt": salt,
        "password_hash": _hash_password(password, salt),
    }


def _load_users_unlocked() -> dict:
    users_file = _users_file()
    if not os.path.exists(users_file):
        return {}
    with open(users_file) as f:
        return json.load(f)


def _save_users_unlocked(users: dict) -> None:
    users_file = _users_file()
    os.makedirs(os.path.dirname(users_file) or ".", exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=os.path.dirname(users_file) or ".", delete=False, suffix=".tmp"
    ) as tmp:
        json.dump(users, tmp, indent=2)
        tmp_path = tmp.name
    os.replace(tmp_path, users_file)


def _verify_password(user: dict, password: str) -> tuple[bool, bool]:
    """
    Return (valid, needs_upgrade).
    Legacy users have salt/password_hash and no algorithm field.
    """
    salt = user.get("salt", "")
    stored_hash = user.get("password_hash", "")
    algorithm = user.get("algorithm")

    if algorithm == "pbkdf2_sha256":
        iterations = int(user.get("iterations", PBKDF2_ITERATIONS))
        candidate = _hash_password(password, salt, iterations)
        return secrets.compare_digest(candidate, stored_hash), False

    candidate = _legacy_hash_password(password, salt)
    return secrets.compare_digest(candidate, stored_hash), True


def register_user(username: str, password: str) -> tuple[bool, str]:
    """
    Register a new user.
    Returns (success: bool, message: str).
    """
    username = _normalize_username(username)
    valid, msg = _validate_username(username)
    if not valid:
        return False, msg

    if not password:
        return False, "Password cannot be empty."

    if len(password) < 6:
        return False, "Password must be at least 6 characters."

    with _users_lock:
        users = _load_users_unlocked()

        if username in users:
            return False, "Username already exists. Please choose another."

        users[username] = _new_password_record(password)
        _save_users_unlocked(users)

    return True, "Account created successfully!"


def login_user(username: str, password: str) -> tuple[bool, str]:
    """
    Verify login credentials.
    Returns (success: bool, message: str).
    """
    username = _normalize_username(username)
    valid, msg = _validate_username(username)
    if not valid:
        return False, msg

    if not password:
        return False, "Password cannot be empty."

    with _users_lock:
        users = _load_users_unlocked()

        if username not in users:
            return False, "Invalid username or password."

        user = users[username]
        is_valid, needs_upgrade = _verify_password(user, password)
        if not is_valid:
            return False, "Invalid username or password."

        if needs_upgrade:
            users[username] = _new_password_record(password)
            _save_users_unlocked(users)

    return True, "Login successful!"
