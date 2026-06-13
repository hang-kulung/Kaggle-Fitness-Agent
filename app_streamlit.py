import asyncio
import threading
import streamlit as st
from main import create_workout_runtime

# ── Persistent background event loop ─────────────────────────────────────────
# A single dedicated thread runs the event loop for the entire process lifetime.
# This ensures aiohttp and all async objects always bind to the same loop.
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


# ── Runtime stored at MODULE level ────────────────────────────────────────────
# Module-level storage survives Streamlit reruns (unlike st.session_state which
# can be recreated), ensuring the runtime and its aiohttp session always live
# on the same background loop.
_runtime = None
_runtime_lock = threading.Lock()


def get_runtime():
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = run_async(create_workout_runtime())
    return _runtime


# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="Workout Trainer Agent", page_icon="💪")
st.title("💪 Workout Trainer Agent")

# ── Init chat history ─────────────────────────────────────────────────────────
if "messages" not in st.session_state:
    st.session_state.messages = [
        {
            "role": "assistant",
            "content": "Hi! Tell me what you want to train today, or ask for today's workout.",
        }
    ]

# ── Init runtime once ─────────────────────────────────────────────────────────
runtime = get_runtime()

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.caption(f"Session: {runtime.session.id}")

    if st.button("Save memory", use_container_width=True):
        run_async(runtime.save_memory())
        st.success("Memory saved.")

    if st.button("Clear screen", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

    if st.button("New session", use_container_width=True):
        # global _runtime
        _runtime = None          # force recreate on next get_runtime()
        st.session_state.messages = []
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
                response = run_async(runtime.send_message(user_input))

            if not response:
                response = "I did not receive a final response. Please try again."
            st.markdown(response)

        st.session_state.messages.append({"role": "assistant", "content": response})
        st.rerun()