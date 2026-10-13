import logging
import os
import streamlit as st
from dotenv import load_dotenv
import requests
import json
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

TEMPERATURE = float(os.getenv("TEMPERATURE", "0.2"))
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "500"))
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "20"))
MAX_INPUT_CHARS = int(os.getenv("MAX_INPUT_CHARS", "4000"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "30"))
MAX_AGENT_STEPS = int(os.getenv("MAX_AGENT_STEPS", "3"))

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

# ---------- Tool: asli weather data (Open-Meteo, free, API key nahi chahiye) ----------
def get_weather(city: str) -> dict:
    try:
        geo = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1},
            timeout=10,
        )
        geo.raise_for_status()
        results = geo.json().get("results")
        if not results:
            return {"error": f"City '{city}' nahi mili"}

        place = results[0]
        weather = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,relative_humidity_2m,precipitation,cloud_cover,wind_speed_10m",
            },
            timeout=10,
        )
        weather.raise_for_status()
        cur = weather.json()["current"]
        return {
            "city": place["name"],
            "country": place.get("country"),
            "temperature_c": cur["temperature_2m"],
            "humidity_percent": cur["relative_humidity_2m"],
            "precipitation_mm": cur["precipitation"],
            "cloud_cover_percent": cur["cloud_cover"],
            "wind_kmh": cur["wind_speed_10m"],
        }
    except requests.RequestException:
        logger.exception("Weather API failed")
        return {"error": "Weather service abhi available nahi hai"}
# LLM ko tool ka description (naam, kaam, arguments)
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Kisi city ka current weather (temperature, humidity, barish, hawa) batata hai",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City ka naam, jaise Noida"}
                },
                "required": ["city"],
            },
        },
    }
]

# naam -> function (LLM naam bolta hai, hum function chalate hain)
AVAILABLE_TOOLS = {"get_weather": get_weather}

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



def run_tool(name: str, raw_args: str) -> dict:
    """Ek tool chalata hai. Fail ho to error dict deta hai, taaki agent crash na ho."""
    func = AVAILABLE_TOOLS.get(name)
    if func is None:
        return {"error": f"Unknown tool: {name}"}
    try:
        return func(**json.loads(raw_args or "{}"))
    except Exception:
        logger.exception("Tool execution failed: %s", name)
        return {"error": "Tool fail hua"}


# ---------- Agent loop + streaming ----------
def stream_agent(client: OpenAI, history: list[dict], collected: list[str], on_tool):
    """Agent loop jo jawab ko tukdon (chunks) mein yield karta hai.

    - Har LLM call stream=True se hoti hai.
    - Text chunks turant yield hote hain (typing effect).
    - Tool call ke chunks jama karke, stream khatam hone par tool chalate hain, phir loop dobara.
    - `collected` mein text jama hota hai, taaki beech mein error aaye to bhi partial jawab bacha rahe.
    """
    # tool ke intermediate messages sirf is request ke liye hain, session history mein save nahi hote
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *history[-MAX_HISTORY_MESSAGES:],
    ]

    for step in range(1, MAX_AGENT_STEPS + 1):
        stream = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            temperature=TEMPERATURE,
            max_tokens=MAX_OUTPUT_TOKENS,
            stream=True,
        )

        text_parts: list[str] = []
        tool_calls: dict[int, dict] = {}  # index -> {id, name, arguments}

        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            # 1) Normal text: turant screen par bhejo
            if delta.content:
                text_parts.append(delta.content)
                collected.append(delta.content)
                yield delta.content

            # 2) Tool call: arguments tukdon mein aate hain, unhe jodte jao
            for tc in delta.tool_calls or []:
                slot = tool_calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function:
                    if tc.function.name:
                        slot["name"] += tc.function.name
                    if tc.function.arguments:
                        slot["arguments"] += tc.function.arguments

        # Tool nahi maanga => ye final jawab tha, loop khatam
        if not tool_calls:
            return

        calls = [tool_calls[i] for i in sorted(tool_calls)]

        # LLM ka tool request history mein add karo
        messages.append(
            {
                "role": "assistant",
                "content": "".join(text_parts) or None,
                "tool_calls": [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {"name": c["name"], "arguments": c["arguments"]},
                    }
                    for c in calls
                ],
            }
        )

        # Har tool chalao aur result wapas messages mein daalo
        for c in calls:
            on_tool(f"🔧 Step {step}: {c['name']}({c['arguments']})")
            result = run_tool(c["name"], c["arguments"])
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": c["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

    # Max steps ho gaye
    fallback = "\n\nMaaf kijiye, kaam poora nahi ho paaya (max steps). Sawal thoda simple karke try karo."
    collected.append(fallback)
    yield fallback


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
        tool_box = st.container()  # tool steps yahan dikhenge (jawab ke upar)
        try:
            st.write_stream(
                stream_agent(
                    client,
                    st.session_state.messages,
                    collected,
                    on_tool=lambda text: tool_box.caption(text),
                )
            )
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