import json
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DEFAULT_LOG = DATA_DIR / "recent_actions.jsonl"
PREVIOUS_LOG = DATA_DIR / "recent_actions.previous.jsonl"

_LOCK = threading.Lock()
MAX_STRING = 4000
MAX_COLLECTION = 40
SENSITIVE_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
)


def _path():
    configured = os.getenv("COMPANION_ACTION_LOG", "").strip()
    return Path(configured) if configured else DEFAULT_LOG


def _safe(value, depth=0):
    if depth > 5:
        return "<max-depth>"

    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, str):
        if len(value) <= MAX_STRING:
            return value
        return value[:MAX_STRING] + "…"

    if isinstance(value, dict):
        result = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_COLLECTION:
                result["<truncated>"] = True
                break
            key_text = str(key)
            lowered = key_text.lower()
            if any(fragment in lowered for fragment in SENSITIVE_FRAGMENTS):
                result[key_text] = "<redacted>"
            else:
                result[key_text] = _safe(item, depth + 1)
        return result

    if isinstance(value, (list, tuple, set)):
        items = list(value)
        safe_items = [_safe(item, depth + 1) for item in items[:MAX_COLLECTION]]
        if len(items) > MAX_COLLECTION:
            safe_items.append("<truncated>")
        return safe_items

    return _safe(str(value), depth + 1)


def start_session(build=None, model=None):
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)

    with _LOCK:
        try:
            if path.exists() and path.stat().st_size > 0:
                shutil.copyfile(path, PREVIOUS_LOG)
        except Exception:
            pass

        try:
            path.write_text("", encoding="utf-8")
        except Exception:
            return

    log_event(
        "host",
        "session_start",
        build=build,
        model=model,
        pid=os.getpid(),
    )


def log_event(component, event, **fields):
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "unix": round(time.time(), 3),
        "component": str(component),
        "event": str(event),
        **{str(key): _safe(value) for key, value in fields.items()},
    }

    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception:
        # Diagnostics must never become a new failure mode for the companion.
        pass


def log_exception(component, event, error, **fields):
    log_event(
        component,
        event,
        error_type=type(error).__name__,
        error=str(error),
        **fields,
    )
