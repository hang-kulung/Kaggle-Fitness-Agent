import asyncio
import hashlib
import threading
import streamlit as st
from auth import register_user, login_user
from main import create_workout_runtime

# ── Persistent background event loop ─────────────────────────────────────────
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_loop_lock = threading.Lock()


def get_loop() -> asyncio.AbstractEventLoop:
    global _loop, _loop_thread
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
            _loop_thread = threading.Thread(target=_loop.run_forever, daemon=True)
            _loop_thread.start()
    return _loop


def run_async(coro):
    """Submit a coroutine to the persistent background loop and block until done."""
    return asyncio.run_coroutine_threadsafe(coro, get_loop()).result(timeout=120)


# ── Per-user runtime registry (module-level) ──────────────────────────────────
# Keyed by username. Lives for the lifetime of the server process so aiohttp
# sessions always stay bound to the same background event loop.
_runtimes: dict = {}
_runtimes_lock = threading.Lock()


def get_runtime_key(user_id: str, api_key: str) -> tuple[str, str]:
    api_key_fingerprint = hashlib.sha256(api_key.encode()).hexdigest()
    return user_id, api_key_fingerprint


def get_runtime(user_id: str, api_key: str):
    runtime_key = get_runtime_key(user_id, api_key)
    with _runtimes_lock:
        if runtime_key not in _runtimes:
            _runtimes[runtime_key] = run_async(
                create_workout_runtime(user_id=user_id, api_key=api_key)
            )
    return _runtimes[runtime_key]


def drop_runtime(user_id: str, api_key: str) -> None:
    with _runtimes_lock:
        _runtimes.pop(get_runtime_key(user_id, api_key), None)


# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="Workout Trainer Agent", page_icon="💪")

# ── Session state defaults ────────────────────────────────────────────────────
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "username" not in st.session_state:
    st.session_state.username = ""
if "api_key" not in st.session_state:
    st.session_state.api_key = ""
if "messages" not in st.session_state:
    st.session_state.messages = []
if "auth_mode" not in st.session_state:
    st.session_state.auth_mode = "Login"   # or "Register"


# ══════════════════════════════════════════════════════════════════════════════
# AUTH SCREEN
# ══════════════════════════════════════════════════════════════════════════════
if not st.session_state.logged_in:
    st.title("💪 Workout Trainer Agent")
    st.subheader("Welcome! Please log in or create an account.")

    # Toggle between Login / Register
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Login", use_container_width=True):
            st.session_state.auth_mode = "Login"
    with col2:
        if st.button("Register", use_container_width=True):
            st.session_state.auth_mode = "Register"

    st.divider()
    mode = st.session_state.auth_mode
    st.markdown(f"### {mode}")

    with st.form("auth_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        api_key = ""
        if mode == "Login":
            api_key = st.text_input(
                "Google API Key",
                type="password",
                help="Your Gemini API key from https://aistudio.google.com/app/apikey",
            )
        submitted = st.form_submit_button(mode, use_container_width=True)

    if submitted:
        if mode == "Login" and not api_key.strip():
            st.error("Please enter your Google API key.")
        else:
            if mode == "Register":
                success, msg = register_user(username, password)
                if success:
                    st.success(msg + " You can now log in.")
                    st.session_state.auth_mode = "Login"
                    st.rerun()
                else:
                    st.error(msg)
            else:
                success, msg = login_user(username, password)
                if success:
                    st.session_state.logged_in = True
                    st.session_state.username  = username.strip().lower()
                    st.session_state.api_key   = api_key.strip()
                    st.session_state.messages  = [
                        {
                            "role": "assistant",
                            "content": f"Hi {username}! Tell me what you want to train today, or ask for today's workout.",
                        }
                    ]
                    st.rerun()
                else:
                    st.error(msg)

    st.stop()   # Don't render anything below while logged out


# ══════════════════════════════════════════════════════════════════════════════
# MAIN APP (only reached when logged in)
# ══════════════════════════════════════════════════════════════════════════════
st.title("💪 Workout Trainer Agent")

# Load (or create) this user's runtime
runtime = get_runtime(
    user_id=st.session_state.username,
    api_key=st.session_state.api_key,
)

# ── Sidebar ───────────────────────────────────────────────────────────────────
# The only addition is the turn-count caption below the session ID.

with st.sidebar:
    st.markdown(f"👤 **{st.session_state.username}**")
    st.caption(f"Session: {runtime.session.id}")

    # Show how many turns are left before automatic session rotation.
    # SESSION_ROTATION_TURNS is imported from main so it stays in sync.
    from main import SESSION_ROTATION_TURNS
    turns_used = runtime.turn_count
    turns_left = SESSION_ROTATION_TURNS - turns_used
    st.caption(f"Context: {turns_used}/{SESSION_ROTATION_TURNS} turns  ({turns_left} left)")

    if st.button("Save memory", use_container_width=True):
        run_async(runtime.save_memory())
        st.success("Memory saved.")

    if st.button("Clear screen", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

    if st.button("New session", use_container_width=True):
        drop_runtime(st.session_state.username, st.session_state.api_key)
        st.session_state.messages = []
        st.rerun()

    st.divider()

    if st.button("Logout", use_container_width=True):
        run_async(runtime.save_memory())
        drop_runtime(st.session_state.username, st.session_state.api_key)
        st.session_state.logged_in = False
        st.session_state.username  = ""
        st.session_state.api_key   = ""
        st.session_state.messages  = []
        st.rerun()

# ── Chat history ──────────────────────────────────────────────────────────────
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# ── Chat input ────────────────────────────────────────────────────────────────
with st.form("chat_form", clear_on_submit=True):
    user_input = st.text_area(
        "Message",
        placeholder="Ask for today's workout, update your goal, or share feedback...",
        height=100,
    )
    submitted = st.form_submit_button("Send", use_container_width=True)

if submitted:
    user_input = user_input.strip()
    if user_input:
        st.session_state.messages.append({"role": "user", "content": user_input})

        with st.chat_message("user"):
            st.markdown(user_input)

        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                try:
                    response = run_async(runtime.send_message(user_input))
                except Exception as exc:
                    response = f"The request failed before the agent could respond: {exc}"

            if not response:
                response = "I did not receive a final response. Please try again."
            st.markdown(response)

        st.session_state.messages.append({"role": "assistant", "content": response})
        st.rerun()
