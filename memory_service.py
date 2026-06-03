"""
memory_service.py  —  Structured, bounded long-term memory.

Files written under ./memory/
  user_profile.json    – age, fitness level, equipment, injuries  (rarely changes)
  preferences.json     – liked/disliked exercises, difficulty prefs
  progress_log.json    – PRs and milestones  (append-only, small entries)
  recent_events.json   – ring-buffer of last MAX_RECENT_EVENTS notable facts

"""

import asyncio
import json
import os
import tempfile
from datetime import datetime

from google.adk.memory.base_memory_service import (
    BaseMemoryService,
    MemoryEntry,
    SearchMemoryResponse,
)
from google.adk.sessions.session import Session
from google.genai import types

MAX_RECENT_EVENTS = 20          # ring-buffer hard cap
DATA_DIR          = "memory"    # all structured files live here


# ── low-level helpers ─────────────────────────────────────────────────────────

def _path(filename: str) -> str:
    return os.path.join(DATA_DIR, filename)


def _read(filename: str, default):
    """Read a JSON file; return *default* on missing or corrupt file."""
    p = _path(filename)
    if not os.path.exists(p):
        return default
    try:
        with open(p) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _write(filename: str, data) -> None:
    """Atomic write: temp-file + os.replace to avoid partial-write corruption."""
    os.makedirs(DATA_DIR, exist_ok=True)
    p = _path(filename)
    with tempfile.NamedTemporaryFile(
        "w", dir=DATA_DIR, delete=False, suffix=".tmp"
    ) as tmp:
        json.dump(data, tmp, indent=2)
        tmp_path = tmp.name
    os.replace(tmp_path, p)


# ── event classifier ──────────────────────────────────────────────────────────

_EVENT_RULES = [
    ("pr",       ["new pr", "personal record", "personal best", "hit a pr"]),
    ("injury",   ["injury", "pain", "discomfort", "hurts", "avoid", "doctor"]),
    ("feedback", ["too easy", "too hard", "increase weight", "decrease weight",
                  "adjust", "felt easy", "felt hard"]),
    ("skipped",  ["skipped", "missed", "couldn't make it", "rest day"]),
    ("goal",     ["new goal", "change goal", "goal is now", "want to focus on"]),
]


def _classify(text: str, role: str) -> dict | None:
    """
    Return a structured event dict if the agent message contains a notable fact,
    else None.  We only index agent (model) messages — they summarise facts
    cleanly; raw user messages are noisy and redundant.
    """
    if role != "model":
        return None
    tl = text.lower()
    for event_type, keywords in _EVENT_RULES:
        if any(kw in tl for kw in keywords):
            return {"type": event_type, "text": text[:300]}
    return None


# ── main service ──────────────────────────────────────────────────────────────

class StructuredMemoryService(BaseMemoryService):
    """
    Replaces JsonFileMemoryService.
    Stores structured facts only; total disk footprint stays small.
    """

    def __init__(self, data_dir: str = DATA_DIR):
        global DATA_DIR
        DATA_DIR = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self._lock = asyncio.Lock()

    # ── write path ────────────────────────────────────────────────────────────

    async def add_session_to_memory(self, session: Session) -> None:
        async with self._lock:
            events: list = _read("recent_events.json", default=[])

            # dedup: skip sessions already ingested
            seen = {e.get("session_id") for e in events}
            if session.id in seen:
                return

            new_events: list[dict] = []
            for ev in session.events:
                if not (ev.content and ev.content.parts):
                    continue
                for part in ev.content.parts:
                    text = (part.text or "").strip()
                    if not text:
                        continue
                    classified = _classify(text, ev.content.role)
                    if classified:
                        new_events.append({
                            "date":       datetime.now().strftime("%Y-%m-%d"),
                            "session_id": session.id,
                            **classified,
                        })

            if not new_events:
                return

            # ring-buffer: keep only the last MAX_RECENT_EVENTS entries
            combined = events + new_events
            _write("recent_events.json", combined[-MAX_RECENT_EVENTS:])

    # ── read path ─────────────────────────────────────────────────────────────

    async def search_memory(
        self, *, app_name: str, user_id: str, query: str
    ) -> SearchMemoryResponse:
        profile     = _read("user_profile.json",  default={})
        preferences = _read("preferences.json",   default={})
        progress    = _read("progress_log.json",  default=[])
        events      = _read("recent_events.json", default=[])

        parts: list[str] = []

        # 1. Always inject the user profile (small, always relevant)
        if profile:
            parts.append("## User profile\n" + json.dumps(profile, indent=2))

        # 2. Preferences
        if preferences:
            parts.append("## Preferences\n" + json.dumps(preferences, indent=2))

        # 3. Last 3 progress milestones
        if progress:
            recent_progress = progress[-3:]
            lines = "\n".join(
                f"- [{p['date']}] {p['text']}" for p in recent_progress
            )
            parts.append(f"## Recent PRs / milestones\n{lines}")

        # 4. Last 5 events (always)
        if events:
            last5 = events[-5:]
            lines = "\n".join(
                f"- [{e['date']}] {e['type']}: {e['text']}" for e in last5
            )
            parts.append(f"## Recent events\n{lines}")

        # 5. Query-matched events from the full ring buffer
        query_words = set(query.lower().split())
        matched = [
            e for e in events
            if any(w in e["text"].lower() for w in query_words)
        ]
        if matched:
            lines = "\n".join(
                f"- [{e['date']}] {e['type']}: {e['text']}"
                for e in matched[:5]
            )
            parts.append(f"## Query-relevant past events\n{lines}")

        if not parts:
            return SearchMemoryResponse(memories=[])

        combined_text = "\n\n".join(parts)
        return SearchMemoryResponse(
            memories=[
                MemoryEntry(
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text=combined_text)],
                    )
                )
            ]
        )
