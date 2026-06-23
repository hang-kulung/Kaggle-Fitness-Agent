"""
api.py  —  FastAPI backend for the Workout Trainer Agent.

Endpoints:
  POST /auth/register      Register a new user
  POST /auth/login         Login and receive a JWT
  GET  /session/info       Return current session ID for logged-in user
  POST /chat               Send a message; streams response as SSE
  GET  /                   Serve the chat UI (static/index.html)
"""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from google.adk.agents import Agent
from google.adk.models.google_llm import Gemini
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.adk.tools import load_memory, FunctionTool
from google.genai import types
from jose import JWTError, jwt
from pydantic import BaseModel

from memory_service import StructuredMemoryService
from user_auth import get_user_data_dir, login_user, register_user
from plan_manager import (
    set_user_context,
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

# ── JWT config ────────────────────────────────────────────────────────────────
JWT_SECRET    = os.getenv("JWT_SECRET", "change-me-in-production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 24 * 7   # 1 week


def create_token(username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS)
    return jwt.encode(
        {"sub": username, "exp": expire},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )


def decode_token(token: str) -> str:
    """Return username or raise HTTPException."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        username: str = payload.get("sub")
        if not username:
            raise ValueError("missing sub")
        return username
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        ) from e


# ── auth dependency ───────────────────────────────────────────────────────────
bearer = HTTPBearer()


def current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
) -> str:
    return decode_token(credentials.credentials)


# ── ADK tools ─────────────────────────────────────────────────────────────────
def get_current_date() -> dict:
    """
    Returns today's date and weekday.
    Use this to know what day to schedule workouts on.
    """
    now = datetime.now()
    return {
        "current_date":  now.strftime("%Y-%m-%d"),
        "weekday":       now.strftime("%A"),
        "weekday_index": now.weekday(),
    }

get_date_tool = FunctionTool(get_current_date)


# lazy import so startup is fast even if googlesearch is slow
def web_search(query: str) -> str:
    """
    Search the web for exercise techniques, injury-safe alternatives,
    or workout information. Returns a summary of the top results.
    """
    try:
        from googlesearch import search as gsearch
        import requests
        from bs4 import BeautifulSoup

        results = []
        for url in gsearch(query, num_results=3, sleep_interval=1):
            try:
                resp = requests.get(url, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
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

web_search_tool = FunctionTool(web_search)

# ── retry config ──────────────────────────────────────────────────────────────
retry_config = types.HttpRetryOptions(
    attempts=5,
    exp_base=2,
    initial_delay=1,
    http_status_codes=[429, 500, 503, 504],
)

# ── agent (singleton) ─────────────────────────────────────────────────────────
workout_agent = Agent(
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

# ── ADK services (singletons) ─────────────────────────────────────────────────
DB_URL   = "sqlite:///workout_agent.db"
APP_NAME = "workout_app"

session_service = DatabaseSessionService(db_url=DB_URL)

# per-user Runner cache:  { username: Runner }
_runners: dict[str, Runner] = {}
# per-user StructuredMemoryService cache
_memory_services: dict[str, StructuredMemoryService] = {}


def get_runner(username: str) -> Runner:
    """Return (or create) the Runner for this user."""
    if username not in _runners:
        user_data_dir = get_user_data_dir(username)
        memory_dir    = os.path.join(user_data_dir, "memory")
        mem_svc       = StructuredMemoryService(memory_dir)
        _memory_services[username] = mem_svc
        _runners[username] = Runner(
            agent=workout_agent,
            app_name=APP_NAME,
            session_service=session_service,
            memory_service=mem_svc,
        )
    return _runners[username]


# ── session helpers ───────────────────────────────────────────────────────────

def _session_id_file(username: str) -> str:
    return os.path.join(get_user_data_dir(username), ".session_id")


async def get_or_create_session(username: str):
    sid_file = _session_id_file(username)
    if os.path.exists(sid_file):
        with open(sid_file) as f:
            sid = f.read().strip()
        try:
            session = await session_service.get_session(
                app_name=APP_NAME, user_id=username, session_id=sid
            )
            if session:
                return session
        except Exception:
            pass

    session = await session_service.create_session(
        app_name=APP_NAME, user_id=username
    )
    with open(sid_file, "w") as f:
        f.write(session.id)
    return session


# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="Workout Trainer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # tighten this for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── request / response models ─────────────────────────────────────────────────

class AuthRequest(BaseModel):
    username: str
    password: str

class ChatRequest(BaseModel):
    message: str


# ── auth endpoints ────────────────────────────────────────────────────────────

@app.post("/auth/register")
async def register(body: AuthRequest):
    ok, err = register_user(body.username, body.password)
    if not ok:
        raise HTTPException(status_code=400, detail=err)
    token = create_token(body.username)
    return {"token": token, "username": body.username}


@app.post("/auth/login")
async def login(body: AuthRequest):
    ok, err = login_user(body.username, body.password)
    if not ok:
        raise HTTPException(status_code=401, detail=err)
    token = create_token(body.username)
    return {"token": token, "username": body.username}


# ── session info endpoint ─────────────────────────────────────────────────────

@app.get("/session/info")
async def session_info(username: str = Depends(current_user)):
    session = await get_or_create_session(username)
    return {"session_id": session.id, "username": username}


# ── chat endpoint (SSE streaming) ─────────────────────────────────────────────

@app.post("/chat")
async def chat(body: ChatRequest, username: str = Depends(current_user)):
    # Set plan_manager's file paths for this user before every request
    set_user_context(username)

    runner  = get_runner(username)
    session = await get_or_create_session(username)

    async def event_stream():
        full_response = []
        try:
            async for event in runner.run_async(
                user_id=username,
                session_id=session.id,
                new_message=types.Content(
                    role="user",
                    parts=[types.Part(text=body.message)],
                ),
            ):
                if event.is_final_response() and event.content:
                    for part in event.content.parts:
                        if part.text:
                            full_response.append(part.text)
                            # SSE format: data: <payload>\n\n
                            chunk = json.dumps({"text": part.text, "done": False})
                            yield f"data: {chunk}\n\n"
                            await asyncio.sleep(0)   # let the event loop breathe

        except Exception as e:
            err_chunk = json.dumps({"error": str(e), "done": True})
            yield f"data: {err_chunk}\n\n"
            return

        # Signal completion
        yield f"data: {json.dumps({'text': '', 'done': True})}\n\n"

        # Persist memory in the background
        updated_session = await session_service.get_session(
            app_name=APP_NAME, user_id=username, session_id=session.id
        )
        if updated_session and username in _memory_services:
            await _memory_services[username].add_session_to_memory(updated_session)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disables nginx buffering if behind a proxy
        },
    )


# ── serve static files (must be last) ────────────────────────────────────────
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")