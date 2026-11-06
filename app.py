import io
import os
import time

import streamlit as st
from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image

from prompt import SYSTEM_PROMPT

load_dotenv()

# ---------- SETTINGS ----------
APP_NAME = "Jehu x ff"
MAX_HISTORY = 8            # last 8 messages are sent (keeps it fast and cheap)
MAX_IMAGE_SIDE = 1280      # charts are resized before sending
MAX_IMAGES = 4             # max charts per message (more = slower)
MAX_BACKUPS = 3            # auto-discovered backup models to try
REQUEST_TIMEOUT_MS = 60000 # give up on one request after 60 seconds
TOTAL_BUDGET_SEC = 170     # stop trying new models after this many seconds

USER_AVATAR = "👤"
BOT_AVATAR = "⛩️"

st.set_page_config(
    page_title=APP_NAME,
    page_icon="⚡",
    layout="centered",
)

# ---------- ANIME THEME ----------
st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Rajdhani:wght@500;700&family=Poppins:wght@400;500&display=swap');

html, body, [class*="css"], .stApp {
    font-family: 'Poppins', sans-serif;
}

.stApp {
    background:
        radial-gradient(circle at 12% 8%, rgba(255, 79, 163, 0.22), transparent 38%),
        radial-gradient(circle at 88% 18%, rgba(0, 229, 255, 0.18), transparent 36%),
        linear-gradient(135deg, #0b0920 0%, #1b1147 52%, #2a0f45 100%);
    background-attachment: fixed;
}

.hero {
    text-align: center;
    padding: 10px 0 4px 0;
}
.hero h1 {
    font-family: 'Rajdhani', sans-serif;
    font-weight: 700;
    font-size: 2.6rem;
    letter-spacing: 2px;
    margin: 0;
    background: linear-gradient(90deg, #ff4fa3, #ffd36e, #00e5ff);
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: transparent;
    filter: drop-shadow(0 0 12px rgba(255, 79, 163, 0.55));
}
.hero p {
    margin: 2px 0 0 0;
    color: #cdbfff;
    font-size: 0.9rem;
    letter-spacing: 1px;
}

[data-testid="stChatMessage"] {
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid rgba(255, 79, 163, 0.35);
    border-radius: 18px;
    backdrop-filter: blur(6px);
    box-shadow: 0 0 18px rgba(255, 79, 163, 0.10);
}

[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #17123a 0%, #0f0b2b 100%);
    border-right: 1px solid rgba(0, 229, 255, 0.25);
}

.stButton > button {
    border-radius: 14px;
    border: 1px solid #ff4fa3;
    background: linear-gradient(90deg, rgba(255, 79, 163, 0.25), rgba(0, 229, 255, 0.20));
    color: #fff;
}
.stButton > button:hover {
    border-color: #00e5ff;
    box-shadow: 0 0 14px rgba(0, 229, 255, 0.5);
}

[data-testid="stChatInput"] {
    border-radius: 18px;
}
</style>
<div class="hero">
    <h1>⚡ Jehu x ff</h1>
    <p>✦ ゴールド・アナリスト · Elite Chart Analyst ✦</p>
</div>
""",
    unsafe_allow_html=True,
)


# ---------- SECRETS / KEYS ----------
def get_secret(name):
    value = os.getenv(name)
    if value:
        return value
    try:
        return st.secrets[name]
    except Exception:
        return None


API_KEY = get_secret("GEMINI_API_KEY")
APP_PASSWORD = get_secret("APP_PASSWORD")

MODEL = get_secret("GEMINI_MODEL") or "gemini-3.6-flash"   # fast model
EXTRA_FALLBACK = get_secret("GEMINI_FALLBACK_MODEL")        # optional manual backup

if not API_KEY:
    st.error("GEMINI_API_KEY not found. Add it to your .env file (or Streamlit Secrets).")
    st.stop()

# ---------- OPTIONAL PASSWORD ----------
if APP_PASSWORD:
    if not st.session_state.get("authed"):
        pw = st.text_input("Password", type="password")
        if pw == APP_PASSWORD:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.stop()

client = genai.Client(
    api_key=API_KEY,
    http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
)

if "messages" not in st.session_state:
    st.session_state.messages = []

# ---------- MODEL DISCOVERY ----------
SKIP_WORDS = (
    "image", "tts", "audio", "live", "embed", "robotics",
    "computer", "veo", "imagen", "lyria", "aqa", "learnlm",
)


def discover_models():
    """All chat-capable Gemini text models your key can see."""
    if "found_models" in st.session_state:
        return st.session_state.found_models
    found = []
    try:
        for m in client.models.list():
            name = (m.name or "").replace("models/", "")
            actions = getattr(m, "supported_actions", None) or []
            if actions and "generateContent" not in actions:
                continue
            low = name.lower()
            if "gemini" not in low:
                continue
            if any(w in low for w in SKIP_WORDS):
                continue
            found.append(name)
    except Exception:
        found = []
    found = sorted(set(found), reverse=True)
    st.session_state.found_models = found
    return found


def flash_models():
    """Only the fast models, used as automatic backups."""
    return [n for n in discover_models() if "flash" in n.lower()]


def model_chain(primary):
    """Primary model first, then manual backup, then auto-discovered backups."""
    chain = [primary]
    if EXTRA_FALLBACK and EXTRA_FALLBACK not in chain:
        chain.append(EXTRA_FALLBACK)
    if MODEL not in chain:
        chain.append(MODEL)
    added = 0
    for name in flash_models():
        if name not in chain and added < MAX_BACKUPS:
            chain.append(name)
            added += 1
    return chain


BUSY_WORDS = ("503", "429", "unavailable", "resource_exhausted", "overloaded", "high demand")
GONE_WORDS = ("404", "not_found", "no longer available", "not found", "403", "permission_denied")
SLOW_WORDS = ("timeout", "timed out", "deadline", "504")


def ask_gemini(contents, primary):
    """Try models in order. Returns (answer, model_used)."""
    start = time.time()
    last_error = None
    for name in model_chain(primary):
        if time.time() - start > TOTAL_BUDGET_SEC:
            break
        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model=name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        temperature=0.3,
                    ),
                )
                return (response.text or "(No answer returned.)"), name
            except Exception as e:
                last_error = e
                msg = str(e).lower()
                if any(w in msg for w in GONE_WORDS):
                    break  # model retired, try the next one
                if any(w in msg for w in SLOW_WORDS):
                    break  # too slow, try the next one
                if any(w in msg for w in BUSY_WORDS):
                    if attempt == 0:
                        time.sleep(3)
                        continue  # busy: one quick retry
                    break
                return f"Error: {e}", name  # a different problem, show it
    return (
        "The AI is busy or slow right now. Please try again in a minute, "
        "or send fewer charts / use Quick signal mode.\n\n"
        f"Details: {last_error}"
    ), primary


# ---------- SIDEBAR ----------
with st.sidebar:
    st.markdown("### ⚙️ Settings")
    style = st.selectbox(
        "Trading style",
        ["Scalp (M1/M5)", "Intraday (M5/M15/H1)", "Swing (H1/H4/D1)"],
        index=0,
    )
    mode = st.radio("Answer mode", ["Full analysis", "Quick signal"], index=0)

    model_options = [MODEL] + [n for n in discover_models() if n != MODEL]
    primary_model = st.selectbox("AI model", model_options, index=0)
    st.caption(
        "Pro models usually need paid billing. If a model fails or is busy, "
        "the app switches to a backup automatically."
    )

    balance = st.number_input(
        "Account balance ($), optional", min_value=0.0, value=0.0, step=100.0
    )
    risk_pct = st.number_input(
        "Risk per trade (%)", min_value=0.0, max_value=5.0, value=1.0, step=0.25
    )

    with st.expander("🔎 Models available to my key"):
        models_found = discover_models()
        if models_found:
            for n in models_found:
                st.write(f"• {n}")
            st.caption("Backups are picked automatically from the Flash models.")
        else:
            st.write("Could not load the list. Check your API key.")

    if st.button("🗑️ New chat"):
        st.session_state.messages = []
        st.rerun()


def prepare_image(uploaded):
    """Resize the chart so it uses fewer tokens, return (bytes, mime)."""
    img = Image.open(uploaded).convert("RGB")
    img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue(), "image/jpeg"


# ---------- SHOW HISTORY ----------
for m in st.session_state.messages:
    avatar = USER_AVATAR if m["role"] == "user" else BOT_AVATAR
    with st.chat_message(m["role"], avatar=avatar):
        st.markdown(m["text"])

# ---------- INPUT ----------
prompt = st.chat_input(
    "Send a chart or ask a question...",
    accept_file="multiple",
    file_type=["png", "jpg", "jpeg", "webp"],
)

if prompt:
    user_text = prompt.text or "Analyze this chart."
    files = list(prompt.files or [])

    if len(files) > MAX_IMAGES:
        st.warning(f"Only the first {MAX_IMAGES} charts were used (more charts = slower).")
        files = files[:MAX_IMAGES]

    with st.chat_message("user", avatar=USER_AVATAR):
        st.markdown(user_text)
        for f in files:
            st.image(f)

    # Settings context sent with this message only
    ctx = f"[App settings] Trading style: {style}. Answer mode: {mode}."
    if balance > 0:
        ctx += f" Account balance: ${balance:,.0f}. Risk per trade: {risk_pct}%."
    ctx += f" Number of images attached: {len(files)}."

    contents = []
    for m in st.session_state.messages[-MAX_HISTORY:]:
        role = "user" if m["role"] == "user" else "model"
        contents.append(types.Content(role=role, parts=[types.Part(text=m["text"])]))

    parts = [types.Part(text=f"{ctx}\n\n{user_text}")]
    for f in files:
        data, mime = prepare_image(f)
        parts.append(types.Part.from_bytes(data=data, mime_type=mime))
    contents.append(types.Content(role="user", parts=parts))

    with st.chat_message("assistant", avatar=BOT_AVATAR):
        with st.spinner("Reading the chart... ✨"):
            answer, used_model = ask_gemini(contents, primary_model)
        st.markdown(answer)
        failed = answer.startswith(("Error", "The AI is busy"))
        if used_model != primary_model and not failed:
            st.caption(f"Answered by backup model: {used_model}")

    note = f" [{len(files)} chart(s) attached]" if files else ""
    st.session_state.messages.append({"role": "user", "text": user_text + note})
    st.session_state.messages.append({"role": "assistant", "text": answer})
