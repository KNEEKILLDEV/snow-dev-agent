import requests
from config.settings import settings


def generate_gemini(messages, api_key):

    prompt = "\n\n".join([
        f"{m.get('role', 'user').upper()}:\n{m.get('content', '')}"
        for m in messages
    ])

    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

    headers = {
        "Content-Type": "application/json"
    }

    body = {
        "generationConfig": {
            "temperature": 0.2,
            "topP": 0.95,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
        },
        "contents": [
            {
                "parts": [{"text": prompt}]
            }
        ]
    }

    request_timeout = getattr(settings, "LLM_REQUEST_TIMEOUT_SECONDS", 180)

    with requests.Session() as session:
        session.trust_env = False
        r = session.post(
            f"{url}?key={api_key}",
            headers=headers,
            json=body,
            timeout=request_timeout,
        )

    r.raise_for_status()

    data = r.json()

    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except:
        raise Exception(f"Gemini API error: {data}")
