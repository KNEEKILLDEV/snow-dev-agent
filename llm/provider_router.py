import os
import random
from dotenv import load_dotenv

from llm.openai_provider import generate_openai
from llm.gemini_provider import generate_gemini
from llm.claude_provider import generate_claude

load_dotenv()


class ModelRouter:

    def __init__(self):

        self.providers = {
            "openai": generate_openai,
            "gemini": generate_gemini,
            "claude": generate_claude
        }

        self.keys = {
            "openai": self._parse_keys(os.getenv("OPENAI_KEYS")),
            "gemini": self._parse_keys(os.getenv("GEMINI_KEYS")),
            "claude": self._parse_keys(os.getenv("CLAUDE_KEYS"))
        }

        # ✅ Default set to OpenAI
        self.default_provider = os.getenv("DEFAULT_MODEL_PROVIDER", "openai")

    def _parse_keys(self, key_string):

        if not key_string:
            return []

        return [k.strip() for k in key_string.split(",") if k.strip()]

    def get_random_key(self, provider):

        keys = self.keys.get(provider, [])

        if not keys:
            raise Exception(f"No API keys configured for provider: {provider}")

        return random.choice(keys)

    def generate(self, messages, provider=None):

        provider = provider or self.default_provider

        if provider not in self.providers:
            raise Exception(f"Unsupported provider: {provider}")

        try:
            api_key = self.get_random_key(provider)
            return self.providers[provider](messages, api_key)

        except Exception as e:

            # 🔁 fallback logic
            fallback_order = ["openai", "gemini", "claude"]

            for p in fallback_order:
                if p == provider:
                    continue

                try:
                    api_key = self.get_random_key(p)
                    return self.providers[p](messages, api_key)
                except:
                    continue

            raise Exception(f"All providers failed: {str(e)}")