import os
from dotenv import load_dotenv

load_dotenv()


def parse_keys(key_string):
    if not key_string:
        return []
    return [k.strip() for k in key_string.split(",") if k.strip()]


class Settings:

    def __init__(self):

        # 🔥 MULTI-KEY SUPPORT
        self.OPENAI_API_KEYS = parse_keys(os.getenv("OPENAI_API_KEYS"))
        self.GEMINI_API_KEYS = parse_keys(os.getenv("GEMINI_API_KEYS"))
        self.CLAUDE_API_KEYS = parse_keys(os.getenv("CLAUDE_API_KEYS"))

        self.DEFAULT_PROVIDER = os.getenv("DEFAULT_PROVIDER", "gemini")

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

        if not self.SN_USERNAME or not self.SN_PASSWORD:
            print("⚠️ SN credentials missing (deploy disabled)")


settings = Settings()