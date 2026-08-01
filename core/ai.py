import re

try:
    from groq import Groq
except ImportError:  # pragma: no cover - depends on environment
    Groq = None

from core.config import GROQ_API_KEY, MODEL
from core.memory import get_recent_messages, save_message
from core.prompts import SYSTEM_PROMPT

client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY and Groq else None


def _clean_reply(reply: str) -> str:
    cleaned = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL).strip()
    cleaned = re.sub(r"</?think>", "", cleaned).strip()
    return cleaned or reply.strip()


def ask_ai(user, system_prompt=None, history=None, max_tokens=800):
    if client is None:
        raise RuntimeError("GROQ_API_KEY is not configured")

    user_id = getattr(user, "id", None)
    username = getattr(user, "name", getattr(user, "username", "User"))
    prompt = getattr(user, "prompt", getattr(user, "content", ""))

    if not prompt:
        raise ValueError("No prompt provided")

    history_rows = history if history is not None else get_recent_messages(user_id)

    messages = [{"role": "system", "content": system_prompt or SYSTEM_PROMPT}]
    for msg in history_rows:
        if isinstance(msg, dict):
            role = msg.get("role", "user")
            content = msg.get("content", "")
        elif hasattr(msg, "keys"):
            role = msg["role"]
            content = msg["content"]
        else:
            role, content = msg[0], msg[1]
        messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": prompt})

    try:
        completion = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.7,
        )
    except Exception:
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.7,
        )

    reply = completion.choices[0].message.content or ""
    reply = _clean_reply(reply)

    save_message(user_id, username, "user", prompt)
    save_message(user_id, "FG-OS", "assistant", reply)

    return reply
