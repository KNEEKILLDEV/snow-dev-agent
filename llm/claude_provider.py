import httpx
import anthropic
from config.settings import settings


def generate_claude(messages, api_key):
    request_timeout = float(getattr(settings, "LLM_REQUEST_TIMEOUT_SECONDS", 180))

    with httpx.Client(trust_env=False, timeout=request_timeout) as http_client:
        client = anthropic.Anthropic(api_key=api_key, http_client=http_client)

        response = client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=1024,
            messages=[
                {
                    "role": m.get("role", "user"),
                    "content": m.get("content", "")
                }
                for m in messages
            ]
        )

    return response.content[0].text.strip()
