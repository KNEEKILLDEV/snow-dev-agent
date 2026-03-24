import google.generativeai as genai


def generate_gemini(messages, api_key):
    try:
        genai.configure(api_key=api_key)

        model = genai.GenerativeModel("gemini-2.5-flash")

        formatted_messages = []

        for m in messages:
            if not isinstance(m, dict):
                continue

            role = m.get("role", "user")
            content = m.get("content", "")

            if role == "system":
                formatted_messages.append({
                    "role": "user",
                    "parts": [f"[SYSTEM]\n{content}"]
                })

            elif role == "assistant":
                formatted_messages.append({
                    "role": "model",
                    "parts": [content]
                })

            else:
                formatted_messages.append({
                    "role": "user",
                    "parts": [content]
                })

        if not formatted_messages:
            raise Exception("No valid messages")

        response = model.generate_content(formatted_messages)

        if hasattr(response, "text") and response.text:
            return response.text.strip()

        if response.candidates:
            return response.candidates[0].content.parts[0].text.strip()

        raise Exception("Empty Gemini response")

    except Exception as e:
        raise Exception(f"Gemini error: {str(e)}")