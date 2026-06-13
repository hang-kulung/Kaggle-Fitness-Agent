import asyncio
import os
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

from plan_manager import (
    save_workout_plan_tool,
    get_workout_plan_tool,
    get_todays_workout_tool,
    update_day_tool,
    update_exercise_tool,
    add_plan_note_tool,
    update_user_profile_tool,
    update_preferences_tool,
    log_progress_tool,
)

load_dotenv()

# ── retry config ──────────────────────────────────────────────────────────────
retry_config = types.HttpRetryOptions(
    attempts=5,
    exp_base=2,
    initial_delay=1,
    http_status_codes=[429, 500, 503, 504],
)

# ── tool functions (defined at module level, wrapped inside create()) ─────────
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
DB_URL   = "sqlite:///workout_agent.db"
APP_NAME = "workout_app"
USER_ID  = "user_001"

# ── session helpers ───────────────────────────────────────────────────────────
async def get_or_create_session(session_service):
    session_id_file = ".session_id"
    if os.path.exists(session_id_file):
        with open(session_id_file) as f:
            sid = f.read().strip()
        try:
            session = await session_service.get_session(
                app_name=APP_NAME, user_id=USER_ID, session_id=sid
            )
            if session:
                return session
        except Exception:
            pass

    session = await session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID
    )
    with open(session_id_file, "w") as f:
        f.write(session.id)
    return session


class WorkoutRuntime:
    """Shared agent runtime used by both terminal and Streamlit interfaces."""

    def __init__(self, runner, memory_service, session):
        self.runner = runner
        self.memory_service = memory_service
        self.session = session

    @classmethod
    async def create(cls):
        # ── Build tools and agent HERE so aiohttp binds to the correct loop ──
        get_date_tool   = FunctionTool(get_current_date)
        web_search_tool = FunctionTool(web_search)

        agent = Agent(
            name="workout_trainer_agent",
            model=Gemini(
                model="gemini-2.5-flash",
                api_key=os.getenv("GOOGLE_API_KEY"),
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
                save_workout_plan_tool,
                get_workout_plan_tool,
                get_todays_workout_tool,
                update_day_tool,
                update_exercise_tool,
                add_plan_note_tool,
                update_user_profile_tool,
                update_preferences_tool,
                log_progress_tool,
            ],
        )

        session_service = DatabaseSessionService(db_url=DB_URL)
        memory_service  = StructuredMemoryService("memory")

        runner = Runner(
            agent=agent,
            app_name=APP_NAME,
            session_service=session_service,
            memory_service=memory_service,
        )

        session = await get_or_create_session(session_service)
        return cls(runner=runner, memory_service=memory_service, session=session)

    async def send_message(self, user_input: str) -> str:
        response_parts = []

        async for event in self.runner.run_async(
            user_id=USER_ID,
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

        self.session = await self.runner.session_service.get_session(
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=self.session.id,
        )
        return "\n".join(response_parts).strip()

    async def save_memory(self) -> None:
        await self.memory_service.add_session_to_memory(self.session)


async def create_workout_runtime() -> WorkoutRuntime:
    return await WorkoutRuntime.create()


# ── main entry point ──────────────────────────────────────────────────────────
async def main():
    print("Workout Trainer Agent  |  type 'exit' to quit, 'save' to save memory\n")

    runtime = await create_workout_runtime()
    print(f"Session: {runtime.session.id}\n")

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