import httpx
from openai import OpenAI
from config.settings import settings


def generate_openai(messages, api_key):
    request_timeout = float(getattr(settings, "LLM_REQUEST_TIMEOUT_SECONDS", 180))

    with httpx.Client(trust_env=False, timeout=request_timeout) as http_client:
        client = OpenAI(api_key=api_key, http_client=http_client)

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages
        )

    return response.choices[0].message.content.strip()
