import json

from llm.router import ModelRouter
from agent.prompts import build_prompt
from config.settings import settings


router = ModelRouter(settings)


def safe_json_parse(content: str):
    """
    Robust JSON parser for LLM responses
    """

    try:
        return json.loads(content)

    except:
        # 🔥 Handle cases where LLM wraps JSON in text
        try:
            start = content.find("{")
            end = content.rfind("}") + 1
            return json.loads(content[start:end])
        except:
            raise Exception("Invalid JSON response from model")


def generate_script(requirement, provider="openai", context="", artifact_hint="auto"):
    """
    Main orchestration pipeline
    """

    # 🧠 Build prompt
    prompt = build_prompt(requirement, context)

    if artifact_hint != "auto":
        prompt += f"\n\nForce artifact type: {artifact_hint}"

    messages = [
        {"role": "system", "content": "You are a ServiceNow expert developer."},
        {"role": "user", "content": prompt},
    ]

    # 🤖 Call LLM via router
    response = router.generate(messages, provider)

    if not response or not response.strip():
        raise Exception("Empty response from model")

    # 🔍 Parse JSON safely
    data = safe_json_parse(response)

    # 🧪 Normalize fields (avoid missing keys issues)
    artifact = {
        "artifact_type": data.get("artifact_type", "business_rule"),
        "name": data.get("name", "Generated Script"),
        "table": data.get("table", "incident"),
        "when": data.get("when", "before"),
        "insert": data.get("insert", True),
        "update": data.get("update", True),
        "script": data.get("script", "")
    }

    if not artifact["script"]:
        raise Exception("Generated script is empty")

    return artifact