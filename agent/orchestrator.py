from agent.prompts import build_prompt, build_table_inference_prompt
from agent.schema import Artifact
from config.settings import settings
from llm.router import ModelRouter
from pydantic import ValidationError
import json
import re
import os

router = ModelRouter(settings)


# ---------------- CLEAN RESPONSE ----------------
def extract_json(text):
    """
    Extract JSON from LLM response (handles ```json blocks)
    """

    if not text:
        return None

    # 🔥 Remove markdown ```json ... ```
    text = re.sub(r"```json\s*", "", text)
    text = re.sub(r"```", "", text)

    text = text.strip()

    # 🔥 Extract JSON block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return match.group(0)

    return text


# ---------------- NORMALIZE TYPE ----------------
def normalize_artifact_type(value):
    if not value:
        return "unknown"

    value = value.lower().strip()

    mapping = {
        "script include": "script_include",
        "script_include": "script_include",

        "business rule": "business_rule",
        "business_rule": "business_rule",

        "client script": "client_script",
        "client_script": "client_script",

        "workflow": "workflow",
        "classic workflow": "workflow",
    }

    return mapping.get(value, "unknown")


def normalize_artifact_hint(value):
    if not value:
        return "auto"

    value = str(value).lower().strip()

    if value == "auto":
        return "auto"

    normalized = normalize_artifact_type(value)

    return normalized if normalized != "unknown" else "auto"


def artifact_requires_table(artifact_type):
    return artifact_type in {"business_rule", "client_script", "workflow"}


def infer_script_artifact_type(payload):
    if not isinstance(payload, dict):
        return "script_include"

    if payload.get("when") is not None or payload.get("insert") is not None or payload.get("update") is not None:
        return "business_rule"

    if payload.get("type"):
        return "client_script"

    return "script_include"


def build_deployable_step(payload, requirement, context, provider, fallback_table=None, step_index=1):
    if not isinstance(payload, dict):
        payload = {}

    step = {
        "artifact_type": normalize_artifact_type(payload.get("artifact_type")),
        "name": payload.get("name") or f"workflow_step_{step_index}",
        "table": payload.get("table"),
        "when": payload.get("when"),
        "insert": payload.get("insert"),
        "update": payload.get("update"),
        "type": payload.get("type"),
        "order": payload.get("order") if payload.get("order") is not None else step_index,
        "description": payload.get("description"),
        "script": payload.get("script"),
    }

    if step["artifact_type"] in {"unknown", "workflow"}:
        step["artifact_type"] = infer_script_artifact_type(payload)

    if artifact_requires_table(step["artifact_type"]) and not step.get("table"):
        inferred_table = fallback_table

        if not inferred_table:
            inferred_table = infer_missing_table(
                requirement=requirement,
                context=context,
                provider=provider,
                artifact_type=step["artifact_type"],
                artifact_name=step.get("name", ""),
                script=step.get("script", ""),
            )

        if inferred_table:
            step["table"] = inferred_table

    return step


def build_workflow_artifact(data, requirement, context, provider):
    if not isinstance(data, dict):
        data = {}

    normalized = dict(data)
    normalized["artifact_type"] = "workflow"

    raw_steps = normalized.get("workflow_steps") or []
    workflow_steps = []

    for index, raw_step in enumerate(raw_steps, start=1):
        if isinstance(raw_step, dict) and normalize_artifact_type(raw_step.get("artifact_type")) == "workflow":
            raise ValueError("Nested workflow steps are not supported yet")

        workflow_steps.append(
            build_deployable_step(
                raw_step,
                requirement=requirement,
                context=context,
                provider=provider,
                fallback_table=normalized.get("table"),
                step_index=index,
            )
        )

    if not workflow_steps and (
        normalized.get("script")
        or normalized.get("when") is not None
        or normalized.get("insert") is not None
        or normalized.get("update") is not None
        or normalized.get("type")
    ):
        workflow_steps.append(
            build_deployable_step(
                normalized,
                requirement=requirement,
                context=context,
                provider=provider,
                fallback_table=normalized.get("table"),
                step_index=1,
            )
        )
        normalized["script"] = None

    if not normalized.get("table") and workflow_steps:
        normalized["table"] = workflow_steps[0].get("table")

    if not normalized.get("table"):
        normalized["table"] = infer_missing_table(
            requirement=requirement,
            context=context,
            provider=provider,
            artifact_type="workflow",
            artifact_name=normalized.get("name", ""),
            script=normalized.get("script", ""),
        )

    if not normalized.get("name"):
        normalized["name"] = "generated_workflow"

    if not normalized.get("description"):
        normalized["description"] = f"Workflow plan for {normalized['name']}"

    if normalized.get("published") is None:
        normalized["published"] = False

    workflow_definition = normalized.get("workflow_definition")
    if not workflow_definition:
        workflow_definition = {
            "goal": requirement,
            "trigger_context": context,
            "step_count": len(workflow_steps),
            "source": "auto-generated workflow plan",
        }

    normalized["workflow_definition"] = workflow_definition
    normalized["workflow_steps"] = workflow_steps
    normalized["script"] = None

    return normalized


def guess_table_from_text(*texts):
    combined = " ".join([str(text or "") for text in texts]).lower()

    patterns = [
        (r"\bincident(s)?\b", "incident"),
        (r"\bproblem(s)?\b", "problem"),
        (r"\bchange request(s)?\b", "change_request"),
        (r"\brequest(ed)? item(s)?\b", "sc_req_item"),
        (r"\bcatalog item request(s)?\b", "sc_req_item"),
        (r"\bsc_req_item\b", "sc_req_item"),
        (r"\buser(s)?\b", "sys_user"),
        (r"\bgroup(s)?\b", "sys_user_group"),
        (r"\bconfiguration item(s)?\b", "cmdb_ci"),
        (r"\bcmdb ci\b", "cmdb_ci"),
        (r"\btask(s)?\b", "task"),
        (r"\bu_[a-z0-9_]+\b", None),
    ]

    for pattern, table in patterns:
        if re.search(pattern, combined):
            if table is None:
                match = re.search(pattern, combined)
                return match.group(0) if match else None
            return table

    return None


def infer_missing_table(requirement, context, provider, artifact_type, artifact_name="", script=""):
    # Prefer deterministic hints first, then let the LLM repair the omission.
    hinted_table = guess_table_from_text(requirement, context, artifact_name, script)
    if hinted_table:
        return hinted_table

    repair_messages = [
        {
            "role": "system",
            "content": "You are a ServiceNow table inference assistant."
        },
        {
            "role": "user",
            "content": build_table_inference_prompt(
                requirement=requirement,
                context=context,
                artifact_type=artifact_type,
                name=artifact_name,
                script=script,
            ),
        }
    ]

    repair_response = router.generate(repair_messages, provider=provider)
    cleaned = extract_json(repair_response)

    try:
        data = json.loads(cleaned)
        table = data.get("table")
        if table and str(table).strip().lower() != "null":
            return str(table).strip()
    except Exception:
        pass

    return None


# ---------------- MAIN FUNCTION ----------------
def generate_script(
    requirement,
    provider="gemini",
    context="",
    artifact_hint="auto",
):
    requested_artifact_type = normalize_artifact_hint(artifact_hint)

    messages = [
        {
            "role": "system",
            "content": "You are a ServiceNow expert developer."
        },
        {
            "role": "user",
            "content": build_prompt(
                requirement=requirement,
                context=context,
                artifact_hint=requested_artifact_type,
            ),
        }
    ]

    response = router.generate(messages, provider=provider)

    print("\n[RAW LLM RESPONSE]\n", response)

    try:
        # 🔥 CLEAN RESPONSE FIRST
        cleaned = extract_json(response)

        print("\n[CLEANED JSON]\n", cleaned)

        data = json.loads(cleaned)

        # 🔥 Normalize type
        model_artifact_type = normalize_artifact_type(data.get("artifact_type"))

        if requested_artifact_type != "auto":
            if model_artifact_type != requested_artifact_type:
                print(
                    f"\n[ARTIFACT TYPE OVERRIDE]\n"
                    f"model={model_artifact_type} requested={requested_artifact_type}"
                )
            data["artifact_type"] = requested_artifact_type
        else:
            data["artifact_type"] = model_artifact_type

        if not data.get("name"):
            data["name"] = "generated_script"

        if requested_artifact_type == "workflow" and model_artifact_type != "workflow":
            source_step = build_deployable_step(
                data,
                requirement=requirement,
                context=context,
                provider=provider,
                fallback_table=data.get("table"),
                step_index=1,
            )

            data = {
                "artifact_type": "workflow",
                "name": data.get("name") or "generated_workflow",
                "table": data.get("table") or source_step.get("table"),
                "description": data.get("description") or f"Workflow plan for {data.get('name', 'generated artifact')}",
                "published": data.get("published") if data.get("published") is not None else False,
                "workflow_definition": {
                    "goal": requirement,
                    "trigger_context": context,
                    "step_count": 1,
                    "source": "auto-wrapped workflow plan",
                    "source_artifact_type": source_step.get("artifact_type"),
                },
                "workflow_steps": [source_step],
                "script": None,
            }

        if data.get("artifact_type") == "workflow":
            data = build_workflow_artifact(
                data=data,
                requirement=requirement,
                context=context,
                provider=provider,
            )
        elif artifact_requires_table(data.get("artifact_type")) and not data.get("table"):
            inferred_table = infer_missing_table(
                requirement=requirement,
                context=context,
                provider=provider,
                artifact_type=data.get("artifact_type"),
                artifact_name=data.get("name", ""),
                script=data.get("script", ""),
            )
            if inferred_table:
                data["table"] = inferred_table

        artifact = Artifact.model_validate(data)

        if artifact.artifact_type == "workflow":
            if not artifact.workflow_steps:
                raise ValueError(
                    "Workflow artifacts require at least one deployable workflow step."
                )
        elif artifact_requires_table(artifact.artifact_type) and not artifact.table:
            raise ValueError(
                "Could not infer a target table from the requirement. "
                "Please restate the requirement with the record type you want to target."
            )

        if artifact.artifact_type in {"business_rule", "script_include", "client_script"} and not artifact.script:
            raise ValueError("Generated artifact is missing script text")

        return artifact.model_dump()

    except ValidationError as e:
        print("\n[SCHEMA ERROR]\n", str(e))

        fallback_data = data if "data" in locals() and isinstance(data, dict) else {}

        return {
            "artifact_type": fallback_data.get(
                "artifact_type",
                requested_artifact_type if requested_artifact_type != "auto" else "unknown",
            ),
            "name": fallback_data.get("name", "generated_script"),
            "script": fallback_data.get("script", response),
            "table": fallback_data.get("table"),
            "when": fallback_data.get("when"),
            "insert": fallback_data.get("insert"),
            "update": fallback_data.get("update"),
            "type": fallback_data.get("type"),
            "order": fallback_data.get("order"),
            "description": fallback_data.get("description"),
            "published": fallback_data.get("published"),
            "workflow_definition": fallback_data.get("workflow_definition"),
            "workflow_steps": fallback_data.get("workflow_steps"),
        }

    except Exception as e:
        print("\n[JSON ERROR]\n", str(e))

        fallback_data = data if "data" in locals() and isinstance(data, dict) else {}

        return {
            "artifact_type": fallback_data.get(
                "artifact_type",
                requested_artifact_type if requested_artifact_type != "auto" else "unknown",
            ),
            "name": fallback_data.get("name", "generated_script"),
            "script": fallback_data.get("script", response),
            "table": fallback_data.get("table"),
            "when": fallback_data.get("when"),
            "insert": fallback_data.get("insert"),
            "update": fallback_data.get("update"),
            "type": fallback_data.get("type"),
            "order": fallback_data.get("order"),
            "description": fallback_data.get("description"),
            "published": fallback_data.get("published"),
            "workflow_definition": fallback_data.get("workflow_definition"),
            "workflow_steps": fallback_data.get("workflow_steps"),
        }
