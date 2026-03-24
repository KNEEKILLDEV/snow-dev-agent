import random

from llm.openai_provider import generate_openai
from llm.gemini_provider import generate_gemini
from llm.claude_provider import generate_claude


def normalize_messages(messages):
    def flatten(items):
        for i in items:
            if isinstance(i, list):
                yield from flatten(i)
            else:
                yield i

    if not isinstance(messages, list):
        return [{"role": "user", "content": str(messages)}]

    flat = list(flatten(messages))

    normalized = []
    for m in flat:
        if isinstance(m, dict):
            normalized.append({
                "role": m.get("role", "user"),
                "content": str(m.get("content", ""))
            })
        else:
            normalized.append({
                "role": "user",
                "content": str(m)
            })

    return normalized


class ModelRouter:

    def __init__(self, settings):
        self.settings = settings

        self.providers = {
            "openai": generate_openai,
            "gemini": generate_gemini,
            "claude": generate_claude
        }

    def get_keys(self, provider):
        return getattr(self.settings, f"{provider.upper()}_API_KEYS", [])

    def try_provider(self, provider, messages):
        keys = self.get_keys(provider)

        if not keys:
            raise Exception(f"No API keys configured for provider: {provider}")

        for key in keys:
            try:
                print(f"[Router] Trying {provider}")
                return self.providers[provider](messages, key)
            except Exception as e:
                print(f"[Router] {provider} failed: {e}")

        raise Exception(f"All keys failed for {provider}")

    def generate(self, messages, provider="openai"):
        messages = normalize_messages(messages)

        print("[Router DEBUG]", messages)

        providers_to_try = [provider] + [p for p in self.providers if p != provider]

        errors = []

        for p in providers_to_try:
            try:
                return self.try_provider(p, messages)
            except Exception as e:
                errors.append(f"{p}: {str(e)}")

        raise Exception("All providers failed: " + " | ".join(errors))