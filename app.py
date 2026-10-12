import logging
import os
import streamlit as st
from dotenv import load_dotenv
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    OpenAI,
    RateLimitError,
)

load_dotenv()

# ---------- Logging (user ka message log nahi karte, privacy ke liye) ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("chat_app")

# ---------- Config (sab kuch env se, code mein hardcode nahi) ----------
MODEL = os.getenv("MODEL_NAME", "gpt-4o-mini")
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT", "You are a helpful assistant. Answer briefly and clearly."
)

TEMPERATURE = float(os.getenv("TEMPERATURE", "0.3"))
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "800"))
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "20"))
MAX_INPUT_CHARS = int(os.getenv("MAX_INPUT_CHARS", "4000"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "30"))

st.set_page_config(page_title="QueryBot: how can I help you?", page_icon="🤖")

def is_streamlit_cloud():
    # Community Cloud app ko /mount/src/<repo> se chalata hai
    return os.getenv("APP_ENV") == "cloud" or os.getcwd().startswith("/mount/src")

KEY_HINT = "Streamlit Secrets (App Settings → Secrets)" if is_streamlit_cloud() else ".env file"

def get_api_key():
    key = os.getenv("OPENAI_API_KEY")  # local (.env)
    if key:
        return key
    try:
        return st.secrets["OPENAI_API_KEY"]  # Streamlit Cloud (Secrets)
    except Exception:
        return None


@st.cache_resource
def get_client() -> OpenAI | None:
    """Client ek baar banta hai aur reuse hota hai. Retry aur timeout built-in."""
    api_key = get_api_key()
    if not api_key:
        return None
    return OpenAI(api_key=api_key, timeout=REQUEST_TIMEOUT, max_retries=2)


def friendly_error(exc: Exception) -> str:
    """Technical error ko user-friendly message mein badalta hai.

    Order important hai: APITimeoutError, APIConnectionError ka subclass hai,
    aur baaki status errors APIStatusError ke subclass hain.
    """
    if isinstance(exc, AuthenticationError):
        return f"API key invalid hai. Ise {KEY_HINT} mein check karo."
    if isinstance(exc, RateLimitError):
        return "Rate limit ya quota khatam ho gaya. Thodi der baad try karo ya billing check karo."
    if isinstance(exc, APITimeoutError):
        return "Request time out ho gayi. Dobara try karo."
    if isinstance(exc, APIConnectionError):
        return "OpenAI se connect nahi ho paaya. Internet check karo."
    if isinstance(exc, BadRequestError):
        return "Request invalid thi (message bahut lamba ho sakta hai ya model naam galat hai)."
    if isinstance(exc, APIStatusError):
        return f"OpenAI server error ({exc.status_code}). Thodi der baad try karo."
    return "Kuch unexpected error aaya. Dobara try karo."


def stream_reply(client: OpenAI, history: list[dict], collected: list[str]):
    """LLM se chunks laata hai. `collected` mein text jama hota rehta hai,
    taaki beech mein error aaye to bhi partial jawab bacha rahe."""
    stream = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            *history[-MAX_HISTORY_MESSAGES:],  # sirf recent history (token/cost control)
        ],
        temperature=TEMPERATURE,
        max_tokens=MAX_OUTPUT_TOKENS,
        stream=True,
    )
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            text = chunk.choices[0].delta.content
            collected.append(text)
            yield text


# ---------- App ----------
st.title("May I Help You?")

client = get_client()
if client is None:
    st.error(f"OPENAI_API_KEY missing hai. Ise {KEY_HINT} mein add karke app restart karo.")
    st.stop()

if "messages" not in st.session_state:
    st.session_state.messages = []

with st.sidebar:
    if st.button("Clear chat"):
        st.session_state.messages = []
        st.rerun()

for m in st.session_state.messages:
    with st.chat_message(m["role"], avatar="🤖" if m["role"] == "assistant" else None):
        st.markdown(m["content"])

prompt = st.chat_input("Search anything...", max_chars=MAX_INPUT_CHARS)

if prompt and prompt.strip():
    prompt = prompt.strip()
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    collected: list[str] = []
    failed = False

    with st.chat_message("assistant", avatar="🤖"):
        try:
            st.write_stream(stream_reply(client, st.session_state.messages, collected))
        except Exception as exc:  # Streamlit ke stop/rerun signals Exception nahi hain
            failed = True
            logger.exception("LLM call failed")
            st.error(friendly_error(exc))

    reply = "".join(collected).strip()

    if reply:
        st.session_state.messages.append({"role": "assistant", "content": reply})
    else:
        # Jawab nahi mila: user message hata do, taaki history clean rahe aur retry ho sake
        st.session_state.messages.pop()
        if not failed:
            st.warning("Clear the chat and retry again. If the problem persists, contact support team.")