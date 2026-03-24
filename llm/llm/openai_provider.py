from openai import OpenAI


def generate_openai(messages, api_key):
    try:
        client = OpenAI(api_key=api_key)

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages
        )

        return response.choices[0].message.content.strip()

    except Exception as e:
        raise Exception(f"OpenAI error: {str(e)}")