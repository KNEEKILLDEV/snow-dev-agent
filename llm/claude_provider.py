import anthropic


def generate_claude(messages, api_key):
    client = anthropic.Anthropic(api_key=api_key)

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