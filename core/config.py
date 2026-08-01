import os

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MODEL = "llama-3.3-70b-versatile"

BIRTHDAY_CHANNEL_ID = int(os.getenv("BIRTHDAY_CHANNEL_ID", "0") or 0)
BIRTHDAY_ROLE_ID = int(os.getenv("BIRTHDAY_ROLE_ID", "0") or 0)
BIRTHDAY_XP_REWARD = int(os.getenv("BIRTHDAY_XP_REWARD", "0") or 0)
BIRTHDAY_COIN_REWARD = int(os.getenv("BIRTHDAY_COIN_REWARD", "0") or 0)
BIRTHDAY_COUNTDOWN_ENABLED = os.getenv("BIRTHDAY_COUNTDOWN_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
BIRTHDAY_INTERVIEW_ENABLED = os.getenv("BIRTHDAY_INTERVIEW_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
BIRTHDAY_ANNOUNCEMENT_ENABLED = os.getenv("BIRTHDAY_ANNOUNCEMENT_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
