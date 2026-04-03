from llm.openai_provider import generate_openai
from llm.gemini_provider import generate_gemini
from llm.claude_provider import generate_claude
from llm.errors import LLMProviderError


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
        if provider not in self.providers:
            raise LLMProviderError(
                provider=provider,
                message=f"Unsupported provider: {provider}. Choose one of: openai, gemini, claude.",
                error_kind="invalid_request",
                raw_error="Provider is not registered in the router.",
            )

        keys = self.get_keys(provider)

        if not keys:
            raise LLMProviderError(
                provider=provider,
                message=f"{provider} is not configured. Add {provider.upper()}_API_KEYS to .env.",
                error_kind="no_keys",
                raw_error="No API keys configured for provider",
            )

        last_error = None
        for key in keys:
            try:
                print(f"[Router] Trying {provider}")
                return self.providers[provider](messages, key)
            except Exception as e:
                print(f"[Router] {provider} failed: {e}")
                last_error = e

        if last_error is None:
            raise LLMProviderError(
                provider=provider,
                message=f"{provider} request failed.",
                raw_error="No provider error was captured.",
            )

        raise LLMProviderError.from_exception(provider, last_error)

    def generate(self, messages, provider=None):
        messages = normalize_messages(messages)

        print("[Router DEBUG]", messages)

        if not provider:
            provider = self.settings.DEFAULT_PROVIDER

        return self.try_provider(provider, messages)
