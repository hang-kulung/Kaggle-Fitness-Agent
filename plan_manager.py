"""
plan_manager.py - Workout plan tools + structured profile/preference/progress tools.

Each agent runtime must call create_workout_tools(plan_file, memory_dir) so the
tools write only to that user's storage directory.
"""

import json
import os
import tempfile
from datetime import datetime
from typing import Callable

from google.adk.tools import FunctionTool


DEFAULT_PLAN_FILE = "workout_plan.json"
DEFAULT_MEMORY_DIR = "memory"


def _read_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: str, data) -> None:
    """Atomic write."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=directory, delete=False, suffix=".tmp"
    ) as tmp:
        json.dump(data, tmp, indent=2)
        tmp_path = tmp.name
    os.replace(tmp_path, path)


def _make_tool_functions(plan_file: str, memory_dir: str) -> list[Callable]:
    os.makedirs(os.path.dirname(plan_file) or ".", exist_ok=True)
    os.makedirs(memory_dir, exist_ok=True)

    def mem_path(filename: str) -> str:
        return os.path.join(memory_dir, filename)

    def load_plan() -> dict:
        return _read_json(plan_file, default={})

    def save_plan(plan: dict) -> None:
        _write_json(plan_file, plan)

    def save_workout_plan(plan: dict) -> dict:
        """
        Save the full 7-day workout plan to persistent storage.
        Call this ONCE after creating a brand-new plan for the user.
        Do NOT call this for small adjustments - use update_exercise or update_day.

        Args:
            plan: Full 7-day workout plan dict. Each exercise should include:
                  name, sets, reps_or_duration, rest_seconds, form_cue.

        Returns:
            Confirmation dict with status and saved_at timestamp.
        """
        plan["saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        plan["version"] = load_plan().get("version", 0) + 1
        save_plan(plan)
        return {
            "status": "saved",
            "version": plan["version"],
            "saved_at": plan["saved_at"],
        }

    def get_workout_plan() -> dict:
        """
        Load the full 7-day workout plan from persistent storage.
        Call this at the start of every session to read the current plan.

        Returns:
            The full plan dict if it exists, or {"status": "no_plan"}.
        """
        plan = load_plan()
        return plan if plan else {"status": "no_plan"}

    def get_todays_workout(weekday: str) -> dict:
        """
        Get only today's workout from the saved plan.

        Args:
            weekday: Full weekday name e.g. 'Monday', 'Tuesday', etc.

        Returns:
            Dict with today's focus and exercises, or no_plan/rest_day status.
        """
        plan = load_plan()
        if not plan:
            return {"status": "no_plan"}

        today = plan.get("days", {}).get(weekday)
        if not today:
            return {"status": "day_not_found", "weekday": weekday}

        if today.get("focus", "").lower() == "rest":
            return {
                "status": "rest_day",
                "weekday": weekday,
                "message": "Today is a rest day. Light stretching is fine.",
            }

        return {
            "status": "ok",
            "weekday": weekday,
            "focus": today["focus"],
            "exercises": today["exercises"],
        }

    def update_day(weekday: str, updated_day: dict) -> dict:
        """
        Replace the workout for a single day.
        Use when a full day needs restructuring.

        Args:
            weekday: Full weekday name e.g. 'Wednesday'.
            updated_day: New day dict with focus and exercises.

        Returns:
            Confirmation with the updated day content.
        """
        plan = load_plan()
        if not plan:
            return {"status": "error", "message": "No plan exists yet. Create one first."}

        plan["days"][weekday] = updated_day
        plan["last_modified"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        plan["version"] = plan.get("version", 0) + 1
        save_plan(plan)
        return {
            "status": "updated",
            "weekday": weekday,
            "new_content": updated_day,
            "version": plan["version"],
        }

    def update_exercise(weekday: str, exercise_name: str, updated_exercise: dict) -> dict:
        """
        Update a single exercise within a day.
        Use for small tweaks: reducing sets, swapping an exercise, adjusting rest time.

        Args:
            weekday: Full weekday name e.g. 'Monday'.
            exercise_name: Exact exercise name, case-insensitive.
            updated_exercise: Dict with any exercise fields to update.

        Returns:
            Confirmation or error if exercise not found.
        """
        plan = load_plan()
        if not plan:
            return {"status": "error", "message": "No plan exists yet."}

        day = plan["days"].get(weekday)
        if not day:
            return {"status": "error", "message": f"Day '{weekday}' not found in plan."}

        exercises = day.get("exercises", [])
        for i, ex in enumerate(exercises):
            if ex.get("name", "").lower() == exercise_name.lower():
                exercises[i].update(updated_exercise)
                plan["days"][weekday]["exercises"] = exercises
                plan["last_modified"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                plan["version"] = plan.get("version", 0) + 1
                save_plan(plan)
                return {
                    "status": "updated",
                    "weekday": weekday,
                    "exercise": exercises[i],
                    "version": plan["version"],
                }

        return {
            "status": "error",
            "message": f"Exercise '{exercise_name}' not found on {weekday}.",
            "available": [e["name"] for e in exercises],
        }

    def add_plan_note(note: str) -> dict:
        """
        Append a short note to the plan log.

        Args:
            note: Free text note.

        Returns:
            Confirmation with all current notes.
        """
        plan = load_plan()
        if not plan:
            return {"status": "error", "message": "No plan exists yet."}

        notes_log = plan.get("notes_log", [])
        notes_log.append({"date": datetime.now().strftime("%Y-%m-%d"), "note": note})
        plan["notes_log"] = notes_log
        save_plan(plan)
        return {"status": "noted", "all_notes": notes_log}

    def update_user_profile(updates: dict) -> dict:
        """
        Persist key facts about the user that should survive across sessions.
        Call when the user shares or changes age, fitness level, equipment,
        injuries, or primary goal.

        Args:
            updates: Dict of profile updates.

        Returns:
            The full updated profile.
        """
        path = mem_path("user_profile.json")
        profile = _read_json(path, default={})
        profile.update(updates)
        profile["last_updated"] = datetime.now().strftime("%Y-%m-%d")
        _write_json(path, profile)
        return {"status": "profile_updated", "profile": profile}

    def update_preferences(updates: dict) -> dict:
        """
        Record the user's workout preferences so future sessions respect them.

        Args:
            updates: Dict of preference updates.

        Returns:
            The full updated preferences dict.
        """
        path = mem_path("preferences.json")
        prefs = _read_json(path, default={})
        updates = dict(updates)

        for list_key in ("liked_exercises", "disliked_exercises", "rest_day_preference"):
            if list_key in updates:
                existing = set(prefs.get(list_key, []))
                existing.update(updates.pop(list_key))
                prefs[list_key] = sorted(existing)

        prefs.update(updates)
        prefs["last_updated"] = datetime.now().strftime("%Y-%m-%d")
        _write_json(path, prefs)
        return {"status": "preferences_updated", "preferences": prefs}

    def log_progress(entry: str) -> dict:
        """
        Append a permanent progress milestone.

        Args:
            entry: One-line description of the milestone.

        Returns:
            Confirmation with the full progress log.
        """
        path = mem_path("progress_log.json")
        log: list = _read_json(path, default=[])
        log.append({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "text": entry,
        })
        _write_json(path, log)
        return {"status": "logged", "progress_log": log}

    return [
        save_workout_plan,
        get_workout_plan,
        get_todays_workout,
        update_day,
        update_exercise,
        add_plan_note,
        update_user_profile,
        update_preferences,
        log_progress,
    ]


def create_workout_tools(plan_file: str, memory_dir: str) -> list[FunctionTool]:
    """Create path-bound FunctionTools for one user's workout runtime."""
    return [FunctionTool(fn) for fn in _make_tool_functions(plan_file, memory_dir)]


# Backward-compatible local functions for direct imports/tests. Runtime code
# should use create_workout_tools() to avoid cross-user data leakage.
(
    save_workout_plan,
    get_workout_plan,
    get_todays_workout,
    update_day,
    update_exercise,
    add_plan_note,
    update_user_profile,
    update_preferences,
    log_progress,
) = _make_tool_functions(DEFAULT_PLAN_FILE, DEFAULT_MEMORY_DIR)
