import asyncio
import os
import re
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
    attempts=2,
    exp_base=2,
    initial_delay=1,
    http_status_codes=[500, 503, 504],
)

# Use configured DATA_DIR when writable, local ./data otherwise.
DATA_DIR = get_data_dir()

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
        "db_url":          f"sqlite:///{user_dir}/workout_agent.db",
        "memory_dir":      memory_dir,
        "plan_file":       os.path.join(user_dir, "workout_plan.json"),
        "session_id_file": os.path.join(user_dir, ".session_id"),
    }


# ── session helpers ───────────────────────────────────────────────────────────
async def get_or_create_session(session_service, user_id: str, session_id_file: str):
    if os.path.exists(session_id_file):
        with open(session_id_file) as f:
            sid = f.read().strip()
        try:
            session = await session_service.get_session(
                app_name=APP_NAME, user_id=user_id, session_id=sid
            )
            if session:
                return session
        except Exception:
            pass

    session = await session_service.create_session(
        app_name=APP_NAME, user_id=user_id
    )
    with open(session_id_file, "w") as f:
        f.write(session.id)
    return session


class WorkoutRuntime:
    """Shared agent runtime — one instance per logged-in user."""

    def __init__(self, runner, memory_service, session, user_id: str):
        self.runner = runner
        self.memory_service = memory_service
        self.session = session
        self.user_id = user_id

    @classmethod
    async def create(cls, user_id: str, api_key: str):
        paths = get_user_paths(user_id)

        # Build tools and agent inside create() so aiohttp binds
        # to the correct event loop (avoids "Future attached to different loop")
        get_date_tool   = FunctionTool(get_current_date)
        web_search_tool = FunctionTool(web_search)
        workout_tools   = create_workout_tools(
            plan_file=paths["plan_file"],
            memory_dir=paths["memory_dir"],
        )

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

        session = await get_or_create_session(
            session_service, user_id, paths["session_id_file"]
        )
        return cls(
            runner=runner,
            memory_service=memory_service,
            session=session,
            user_id=user_id,
        )

    async def send_message(self, user_input: str) -> str:
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
        except Exception as exc:
            return format_model_error(exc)

        await self.refresh_session()
        return "\n".join(response_parts).strip()

    async def save_memory(self) -> None:
        await self.memory_service.add_session_to_memory(self.session)

    async def refresh_session(self) -> None:
        self.session = await self.runner.session_service.get_session(
            app_name=APP_NAME,
            user_id=self.user_id,
            session_id=self.session.id,
        )


def format_model_error(exc: Exception) -> str:
    error_text = str(exc)
    if "RESOURCE_EXHAUSTED" in error_text or "429" in error_text:
        return (
            "Your Gemini API key has hit its current quota for this model. "
            "Please wait and try again, use a different API key, or check the "
            "quota/billing settings for the Google AI Studio project that owns this key."
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


if __name__ == "__main__":
    asyncio.run(main())
