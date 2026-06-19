## Workout Trainer Agent

This project uses Google ADK to create a personal workout trainer agent with
persistent workout plans, structured memory, and a Streamlit web UI.

It was originally built as practice for the **5-Day AI Agents Intensive Course
with Google** presented by Kaggle.

---

## Installation

This project was built against **Python 3.14.0**.

Create a virtual environment, then install dependencies:

```bash
pip install -r requirements.txt
```

---

## Run Locally

Terminal app:

```bash
python3 main.py
```

Streamlit UI:

```bash
streamlit run app_streamlit.py
```

Users create an account, then enter their own Gemini API key when logging in.
API keys are kept only in the Streamlit session and are not written to disk.

---

## Storage

Set `DATA_DIR` to control where user data is stored:

```bash
export DATA_DIR=data
```

Each user gets isolated storage under `DATA_DIR/<username>/`, including:

- `workout_plan.json`
- `memory/`
- `workout_agent.db`
- `.session_id`

The shared auth file is stored at `DATA_DIR/users.json`.

---

## Hosting

For Hugging Face Spaces, use `app_streamlit.py` as the Streamlit entrypoint.
If persistent storage is available, set:

```bash
DATA_DIR=/data
```

If no persistent storage is configured, the app falls back to local `data/`,
which may be reset by the host.
