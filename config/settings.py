import os
from dotenv import load_dotenv

load_dotenv()


def parse_keys(key_string):
    if not key_string:
        return []
    return [k.strip() for k in key_string.split(",") if k.strip()]


def parse_int(value, default):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default


def getenv_any(*names, default=None):
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


class Settings:

    def __init__(self):

        # 🔥 MULTI-KEY SUPPORT
        self.OPENAI_API_KEYS = parse_keys(getenv_any("OPENAI_API_KEYS", "OPENAI_KEYS"))
        self.GEMINI_API_KEYS = parse_keys(getenv_any("GEMINI_API_KEYS", "GEMINI_KEYS"))
        self.CLAUDE_API_KEYS = parse_keys(getenv_any("CLAUDE_API_KEYS", "CLAUDE_KEYS"))
        self.LLM_REQUEST_TIMEOUT_SECONDS = max(
            parse_int(getenv_any("LLM_REQUEST_TIMEOUT_SECONDS"), 180),
            1,
        )

        self.DEFAULT_PROVIDER = getenv_any("DEFAULT_PROVIDER", "DEFAULT_MODEL_PROVIDER", default="gemini")

        # 🔥 ServiceNow
        self.SN_INSTANCE = os.getenv("SN_INSTANCE")
        self.SN_USERNAME = os.getenv("SN_USERNAME")
        self.SN_PASSWORD = os.getenv("SN_PASSWORD")

        self.SN_CLIENT_ID = os.getenv("SN_CLIENT_ID")
        self.SN_CLIENT_SECRET = os.getenv("SN_CLIENT_SECRET")

        # ✅ Validate critical config
        self._validate()

    def _validate(self):
        if not any([
            self.OPENAI_API_KEYS,
            self.GEMINI_API_KEYS,
            self.CLAUDE_API_KEYS
        ]):
            raise Exception("❌ No LLM API keys configured")

        if not self.SN_INSTANCE:
            print("⚠️ SN_INSTANCE not set (deploy will fail)")

        if not self.SN_CLIENT_ID or not self.SN_CLIENT_SECRET:
            print("⚠️ SN client credentials missing (deploy will fail)")

        if not self.SN_USERNAME or not self.SN_PASSWORD:
            print("⚠️ SN username/password missing (not used by current deploy flow)")


settings = Settings()
