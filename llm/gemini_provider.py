import requests


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
        "contents": [
            {
                "parts": [{"text": prompt}]
            }
        ]
    }

    r = requests.post(f"{url}?key={api_key}", headers=headers, json=body)

    data = r.json()

    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except:
        raise Exception(f"Gemini API error: {data}")