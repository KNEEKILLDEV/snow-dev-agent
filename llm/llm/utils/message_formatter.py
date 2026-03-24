def normalize_messages(messages):
    """
    Deep flatten + normalize messages into:
    [{"role": "...", "content": "..."}]
    """

    def flatten(items):
        for item in items:
            if isinstance(item, list):
                yield from flatten(item)
            else:
                yield item

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


def validate_messages(messages):
    if not isinstance(messages, list):
        raise ValueError("messages must be a list")

    for m in messages:
        if not isinstance(m, dict):
            raise ValueError(f"Invalid message format: {m}")

        if "content" not in m:
            raise ValueError(f"Missing content: {m}")