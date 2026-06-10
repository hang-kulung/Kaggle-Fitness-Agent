import asyncio

import streamlit as st

from main import create_workout_runtime


def run_async(coro):
    return asyncio.run(coro)


async def get_session_id():
    runtime = await create_workout_runtime()
    return runtime.session.id


async def send_message(user_input: str):
    runtime = await create_workout_runtime()
    response = await runtime.send_message(user_input)
    return response, runtime.session.id


async def save_memory():
    runtime = await create_workout_runtime()
    await runtime.save_memory()
    return runtime.session.id


st.set_page_config(page_title="Workout Trainer Agent", page_icon="W")
st.title("Workout Trainer Agent")

if "messages" not in st.session_state:
    st.session_state.messages = [
        {
            "role": "assistant",
            "content": "Hi. Tell me what you want to train today, or ask for today's workout.",
        }
    ]

with st.sidebar:
    session_id = run_async(get_session_id())
    st.caption(f"Session: {session_id}")

    if st.button("Save memory", use_container_width=True):
        run_async(save_memory())
        st.success("Memory saved.")

    if st.button("Clear screen", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

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
                response, session_id = run_async(send_message(user_input))
            if not response:
                response = "I did not receive a final response. Please try again."
            st.markdown(response)

        st.session_state.messages.append({"role": "assistant", "content": response})
        st.rerun()
