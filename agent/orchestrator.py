import json
from agent.prompts import build_prompt
from agent.schema import Artifact
from llm.provider_router import ModelRouter
from rag.retriever import retrieve_context

router = ModelRouter()


def safe_json_parse(content: str):
    """
    Extract JSON safely even if model returns extra text
    """

    try:
        return json.loads(content)
    except:
        pass

    # Try extracting JSON block
    try:
        start = content.find("{")
        end = content.rfind("}") + 1

        if start != -1 and end != -1:
            return json.loads(content[start:end])

    except:
        pass

    raise Exception("Failed to parse model output as JSON")


def generate_script(requirement: str, provider: str = "openai") -> Artifact:
    """
    Main orchestration function
    """

    # ---------------- RAG ---------------- #
    context = retrieve_context(requirement)

    # ---------------- Prompt ---------------- #
    prompt = build_prompt(requirement, context)

    messages = [
        {"role": "system", "content": "You are a ServiceNow expert developer."},
        {"role": "user", "content": prompt},
    ]

    # ---------------- LLM ---------------- #
    content = router.generate(messages, provider)

    # ---------------- Parse ---------------- #
    data = safe_json_parse(content)

    # ---------------- Normalize ---------------- #
    data.setdefault("artifact_type", "business_rule")
    data.setdefault("name", "Generated Artifact")
    data.setdefault("script", "")

    # Optional fields (safe defaults)
    data.setdefault("table", "incident")
    data.setdefault("when", "before")
    data.setdefault("insert", True)
    data.setdefault("update", False)

    return Artifact(**data)