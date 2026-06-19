import os
import tempfile

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv():
        return False


DEFAULT_DATA_DIR = "data"


def get_data_dir() -> str:
    """
    Return a writable data directory.

    Deployment configs sometimes set DATA_DIR to paths like /app/data. That can
    be correct on a host but unwritable on a local machine, so local runs fall
    back to ./data instead of crashing during login/runtime creation.
    """
    load_dotenv()
    configured = os.environ.get("DATA_DIR", DEFAULT_DATA_DIR).strip() or DEFAULT_DATA_DIR
    return _ensure_writable(configured) or _ensure_writable(DEFAULT_DATA_DIR) or DEFAULT_DATA_DIR


def _ensure_writable(path: str) -> str | None:
    try:
        os.makedirs(path, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path, delete=True):
            pass
        return path
    except OSError:
        return None
