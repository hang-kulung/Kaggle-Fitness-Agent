import asyncio
import os
import re
import time
from datetime import datetime

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.models.google_llm import Gemini
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.adk.tools import load_memory, FunctionTool
from google.genai import types
from memory_service import StructuredMemoryService
from googlesearch import search as gsearch
import requests
from bs4 import BeautifulSoup

from plan_manager import create_workout_tools
from storage_config import get_data_dir

load_dotenv()

# ── retry config ──────────────────────────────────────────────────────────────
retry_config = types.HttpRetryOptions(
    attempts=5,
    exp_base=2,
    initial_delay=4,
    http_status_codes=[429, 500, 503, 504],
)

# How many user turns before we rotate to a fresh ADK session.
SESSION_ROTATION_TURNS = 8

DATA_DIR = get_data_dir()

# ── RPM rate limiter ──────────────────────────────────────────────────────────
# Free-tier Gemini Flash = 15 requests/minute.
# Each agent turn fires: load_memory → get_workout_plan → get_current_date →
# (maybe 2-3 more tool calls) → final model call = 5-6 API hits in < 1 second.
# This limiter enforces a minimum gap between consecutive Gemini calls so we
# never burst past the RPM ceiling regardless of what the agent decides to do.
#
# MIN_CALL_INTERVAL = 60s / 15 RPM = 4 s between calls (free tier)
# Set to 0 if you have a paid key with higher quota.
MIN_CALL_INTERVAL = 4.0   # seconds — reduce to 1.0 for paid keys

_last_call_time: float = 0.0
# NOTE: do NOT create asyncio.Lock() at module level — it binds to whichever
# event loop is running at import time, which is wrong in Streamlit's
# multi-loop setup. Instead we create it lazily inside the running loop.
_rate_limit_lock: asyncio.Lock | None = None
_rate_limit_lock_loop = None  # track which loop the lock belongs to


def _get_rate_limit_lock() -> asyncio.Lock:
    """Return a rate-limit lock that belongs to the current running loop."""
    global _rate_limit_lock, _rate_limit_lock_loop
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    if _rate_limit_lock is None or _rate_limit_lock_loop is not current_loop:
        _rate_limit_lock = asyncio.Lock()
        _rate_limit_lock_loop = current_loop
    return _rate_limit_lock


async def _rate_limited_sleep() -> None:
    """Sleep just long enough to stay under the RPM limit."""
    global _last_call_time
    lock = _get_rate_limit_lock()
    async with lock:
        now = time.monotonic()
        wait = MIN_CALL_INTERVAL - (now - _last_call_time)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call_time = time.monotonic()


def rate_limited(fn):
    """
    Decorator that wraps a sync tool function with the RPM rate limiter.
    The wrapper is async so ADK can await it; the inner fn stays sync.
    """
    async def wrapper(*args, **kwargs):
        await _rate_limited_sleep()
        # Use get_running_loop() — safe in both single and multi-loop setups
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
    wrapper.__name__        = fn.__name__
    wrapper.__doc__         = fn.__doc__
    wrapper.__annotations__ = getattr(fn, "__annotations__", {})
    return wrapper


# ── tool functions ────────────────────────────────────────────────────────────
def get_current_date() -> dict:
    """
    Returns today's date and weekday.
    Use this to know what day to schedule workouts on.

    Returns:
        current_date: YYYY-MM-DD string
        weekday: full name e.g. 'Monday'
        weekday_index: 0=Monday ... 6=Sunday
    """
    now = datetime.now()
    return {
        "current_date":  now.strftime("%Y-%m-%d"),
        "weekday":       now.strftime("%A"),
        "weekday_index": now.weekday(),
    }


def web_search(query: str) -> str:
    """
    Search the web for exercise techniques, injury-safe alternatives,
    or workout information. Returns a summary of the top results.

    Args:
        query: The search query string.

    Returns:
        A text summary of the top search results.
    """
    try:
        results = []
        for url in gsearch(query, num_results=3, sleep_interval=1):
            try:
                resp = requests.get(
                    url, timeout=5, headers={"User-Agent": "Mozilla/5.0"}
                )
                soup = BeautifulSoup(resp.text, "html.parser")
                paragraphs = [
                    p.get_text().strip()
                    for p in soup.find_all("p")
                    if len(p.get_text().strip()) > 60
                ][:3]
                if paragraphs:
                    results.append(f"Source: {url}\n" + "\n".join(paragraphs))
            except Exception:
                continue
        return "\n\n---\n\n".join(results) if results else "No results found."
    except Exception as e:
        return f"Search failed: {e}"


# ── constants ─────────────────────────────────────────────────────────────────
APP_NAME = "workout_app"
USERNAME_RE = re.compile(r"^[a-z0-9_-]{3,32}$")


# ── per-user path helpers ─────────────────────────────────────────────────────
def get_user_paths(user_id: str) -> dict:
    """Return db and memory paths namespaced to this user."""
    if not USERNAME_RE.fullmatch(user_id):
        raise ValueError(
            "Username must be 3-32 characters using lowercase letters, "
            "numbers, underscores, or hyphens."
        )
    user_dir = os.path.join(DATA_DIR, user_id)
    memory_dir = os.path.join(user_dir, "memory")
    os.makedirs(user_dir, exist_ok=True)
    os.makedirs(memory_dir, exist_ok=True)
    return {
        "db_url":     f"sqlite:///{user_dir}/workout_agent.db",
        "memory_dir": memory_dir,
        "plan_file":  os.path.join(user_dir, "workout_plan.json"),
    }


# ── session helpers ───────────────────────────────────────────────────────────
async def create_new_session(session_service, user_id: str):
    """Always creates a fresh ADK session (no file persistence)."""
    return await session_service.create_session(
        app_name=APP_NAME, user_id=user_id
    )


def _count_user_turns(session) -> int:
    """Count how many user messages are in the current session."""
    return sum(
        1 for ev in (session.events or [])
        if ev.content and ev.content.role == "user"
    )


class WorkoutRuntime:
    """Shared agent runtime — one instance per logged-in user."""

    def __init__(self, runner, memory_service, session, user_id: str, session_service):
        self.runner = runner
        self.memory_service = memory_service
        self.session = session
        self.user_id = user_id
        self._session_service = session_service

    @classmethod
    async def create(cls, user_id: str, api_key: str):
        paths = get_user_paths(user_id)

        # Wrap every tool with the rate limiter so no burst of tool calls
        # can exceed MIN_CALL_INTERVAL between consecutive Gemini requests.
        get_date_tool   = FunctionTool(rate_limited(get_current_date))
        web_search_tool = FunctionTool(rate_limited(web_search))

        # plan_manager tools are sync functions — wrap them too
        raw_workout_tools = create_workout_tools(
            plan_file=paths["plan_file"],
            memory_dir=paths["memory_dir"],
        )
        workout_tools = [
            FunctionTool(rate_limited(ft.func)) for ft in raw_workout_tools
        ]

        agent = Agent(
            name="workout_trainer_agent",
            model=Gemini(
                model="gemini-2.0-flash",
                api_key=api_key,
                retry_options=retry_config,
            ),
            description="Personal workout planner with memory of past sessions.",
            instruction="""
You are a personal workout trainer agent. Your job is to create and manage
a personalised 7-day workout plan and adapt it over time.

═══ STRUCTURED MEMORY (read this first) ═══════════════════════════════════════
You have access to persistent structured memory files. Always use these tools
to record facts — NEVER rely on conversation history to remember user details.

  update_user_profile  – Call when user tells you their age, fitness level,
                         equipment, injuries, or primary goal (first session
                         AND whenever any of these change).

  update_preferences   – Call when user expresses likes, dislikes, preferred
                         duration, or rest-day preferences.

  log_progress         – Call when user hits a PR or significant milestone.
                         Example: "Deadlifted 120 kg for first time."

═══ PLAN MANAGEMENT ═══════════════════════════════════════════════════════════
  - Call get_workout_plan at the START of every session to load the plan.
  - If no plan exists (status: no_plan), gather profile info, call
    update_user_profile, then create a plan and call save_workout_plan.
  - Call get_todays_workout (with today's weekday) to show today's session.
  - Small tweaks  → call update_exercise (do NOT rewrite the whole plan).
  - Full-day restructure → call update_day for that day only.
  - Session observations → call add_plan_note.
  - Only call save_workout_plan again if the user explicitly asks to reset
    or completely redo the plan.

═══ RETURNING USER ════════════════════════════════════════════════════════════
  - Call load_memory first — it returns the user profile, preferences,
    recent PRs, and last few notable events in one block.
  - Acknowledge what changed or what they did last time before today's session.

═══ EVERY SESSION ═════════════════════════════════════════════════════════════
  - Call get_current_date to know today's weekday.
  - Show ONLY today's workout unless the user asks for a different day.
  - Each exercise must include: sets × reps (or duration), rest time, form cue.
  - Never change the 7-day plan unless the user explicitly requests it.

═══ FEEDBACK HANDLING ═════════════════════════════════════════════════════════
  - Too easy / too hard → call update_exercise, then call update_preferences
    if it reflects a general preference.
  - New injury → call update_user_profile (add to injuries list) AND
    update_day / update_exercise as needed.
  - PR / milestone → call log_progress immediately.

═══ RULES ═════════════════════════════════════════════════════════════════════
  - No diet advice.
  - Keep suggestions safe — recommend seeing a doctor for any pain.
  - Use web_search only when you need exercise technique or injury-safe alternatives.
""",
            tools=[
                get_date_tool,
                load_memory,
                web_search_tool,
                *workout_tools,
            ],
        )

        session_service = DatabaseSessionService(db_url=paths["db_url"])
        memory_service  = StructuredMemoryService(paths["memory_dir"])

        runner = Runner(
            agent=agent,
            app_name=APP_NAME,
            session_service=session_service,
            memory_service=memory_service,
        )

        session = await create_new_session(session_service, user_id)
        return cls(
            runner=runner,
            memory_service=memory_service,
            session=session,
            user_id=user_id,
            session_service=session_service,
        )

    async def _rotate_session_if_needed(self) -> None:
        """
        Save memory and start a fresh ADK session once the current one
        exceeds SESSION_ROTATION_TURNS user turns, preventing context
        window overflow on long Streamlit conversations.
        """
        if _count_user_turns(self.session) >= SESSION_ROTATION_TURNS:
            await self.memory_service.add_session_to_memory(self.session)
            self.session = await create_new_session(
                self._session_service, self.user_id
            )

    async def send_message(self, user_input: str) -> str:
        await self._rotate_session_if_needed()

        # Retry loop — ADK's HttpRetryOptions doesn't catch quota errors
        # returned in the response body, so we handle 429 / RESOURCE_EXHAUSTED
        # ourselves with exponential backoff.
        max_attempts = 5
        backoff = 15  # seconds — free tier resets quota every 60s

        for attempt in range(1, max_attempts + 1):
            response_parts = []
            try:
                async for event in self.runner.run_async(
                    user_id=self.user_id,
                    session_id=self.session.id,
                    new_message=types.Content(
                        role="user",
                        parts=[types.Part(text=user_input)],
                    ),
                ):
                    if event.is_final_response() and event.content:
                        for part in event.content.parts:
                            if part.text:
                                response_parts.append(part.text)

                # Success — break out of retry loop
                await self.refresh_session()
                return "\n".join(response_parts).strip()

            except Exception as exc:
                err = str(exc)
                is_quota = "RESOURCE_EXHAUSTED" in err or "429" in err
                if is_quota and attempt < max_attempts:
                    wait = backoff * attempt  # 15s, 30s, 45s, 60s
                    await asyncio.sleep(wait)
                    continue
                return format_model_error(exc)

    async def save_memory(self) -> None:
        await self.memory_service.add_session_to_memory(self.session)

    async def refresh_session(self) -> None:
        self.session = await self._session_service.get_session(
            app_name=APP_NAME,
            user_id=self.user_id,
            session_id=self.session.id,
        )

    @property
    def turn_count(self) -> int:
        """Current number of user turns in the active session."""
        return _count_user_turns(self.session)


def format_model_error(exc: Exception) -> str:
    error_text = str(exc)
    if "RESOURCE_EXHAUSTED" in error_text or "429" in error_text:
        return (
            "⚠️ **Gemini API quota exhausted** after retrying.\n\n"
            "Free tier limits:\n"
            "- **15 requests/minute** (RPM) — resets after 60 seconds\n"
            "- **1,500 requests/day** (RPD) — resets at midnight Pacific time\n\n"
            "If you've been testing heavily, you may have hit the daily limit. "
            "Wait a minute and try again, or check your quota at "
            "https://aistudio.google.com"
        )
    if "API_KEY_INVALID" in error_text or "PERMISSION_DENIED" in error_text:
        return (
            "The Gemini API key was rejected. Please log out, log back in, "
            "and enter a valid Google AI Studio API key."
        )
    return f"The model request failed: {exc}"


async def create_workout_runtime(user_id: str, api_key: str) -> WorkoutRuntime:
    return await WorkoutRuntime.create(user_id=user_id, api_key=api_key)


# ── terminal entry point ──────────────────────────────────────────────────────
async def main():
    print("Workout Trainer Agent  |  type 'exit' to quit, 'save' to save memory\n")

    user_id = input("Username: ").strip().lower()
    api_key = input("Google API Key: ").strip()

    runtime = await create_workout_runtime(user_id=user_id, api_key=api_key)
    print(f"\nSession: {runtime.session.id}\n")

    while True:
        user_input = input("You: ").strip()
        if not user_input:
            continue

        if user_input.lower() in ("exit", "quit"):
            await runtime.save_memory()
            print("[Memory saved] Goodbye!")
            break

        if user_input.lower() == "save":
            await runtime.save_memory()
            print("[Memory saved]")
            continue

        print("Agent: ", end="", flush=True)
        response = await runtime.send_message(user_input)
        print(response)
        print(f"  [turn {runtime.turn_count}/{SESSION_ROTATION_TURNS}]")


if __name__ == "__main__":
    asyncio.run(main())