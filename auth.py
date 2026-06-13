import json
import os
import hashlib
import secrets

USERS_FILE = os.path.join(os.environ.get("DATA_DIR", "."), "users.json")


def _hash_password(password: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}{password}".encode()).hexdigest()


def _load_users() -> dict:
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE) as f:
        return json.load(f)


def _save_users(users: dict) -> None:
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)


def register_user(username: str, password: str) -> tuple[bool, str]:
    """
    Register a new user.
    Returns (success: bool, message: str).
    """
    username = username.strip().lower()

    if not username or not password:
        return False, "Username and password cannot be empty."

    if len(username) < 3:
        return False, "Username must be at least 3 characters."

    if len(password) < 6:
        return False, "Password must be at least 6 characters."

    users = _load_users()

    if username in users:
        return False, "Username already exists. Please choose another."

    salt = secrets.token_hex(16)
    hashed = _hash_password(password, salt)

    users[username] = {"salt": salt, "password_hash": hashed}
    _save_users(users)

    return True, "Account created successfully!"


def login_user(username: str, password: str) -> tuple[bool, str]:
    """
    Verify login credentials.
    Returns (success: bool, message: str).
    """
    username = username.strip().lower()

    if not username or not password:
        return False, "Username and password cannot be empty."

    users = _load_users()

    if username not in users:
        return False, "Invalid username or password."

    user = users[username]
    hashed = _hash_password(password, user["salt"])

    if hashed != user["password_hash"]:
        return False, "Invalid username or password."

    return True, "Login successful!"