import json
import re

MIN_WORKFLOW_STEPS = 3
PLACEHOLDER_TOKENS = ("YOUR_", "REPLACE_ME", "TODO")


def extract_json_blob(text):
    if not isinstance(text, str):
        return None

    cleaned = text.strip()
    cleaned = re.sub(r"^```json\s*", "", cleaned)
    cleaned = re.sub(r"^```\s*", "", cleaned)
    cleaned = re.sub(r"```$", "", cleaned).strip()

    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)

    try:
        parsed = json.loads(cleaned)
    except Exception:
        return None

    return parsed if isinstance(parsed, dict) else None


def iter_strings(value):
    if isinstance(value, str):
        yield value
        return

    if isinstance(value, dict):
        for item in value.values():
            yield from iter_strings(item)
        return

    if isinstance(value, list):
        for item in value:
            yield from iter_strings(item)


def normalize_artifact_type(value):
    if not value:
        return "unknown"

    value = str(value).lower().strip()

    mapping = {
        "business rule": "business_rule",
        "business_rule": "business_rule",
        "script include": "script_include",
        "script_include": "script_include",
        "client script": "client_script",
        "client_script": "client_script",
        "workflow plan": "workflow_plan",
        "workflow_plan": "workflow_plan",
        "workflow": "workflow",
        "classic workflow": "workflow",
    }

    return mapping.get(value, "unknown")


_APPROVAL_MONITOR_TERMS = (
    "monitor",
    "monitoring",
    "evaluate",
    "evaluation",
    "quorum",
    "approval status",
    "approval response",
    "approval responses",
    "review approval",
    "response",
    "responses",
)

_APPROVAL_FINALIZER_TERMS = (
    "finalize",
    "finalise",
    "final",
    "outcome",
    "close",
    "complete",
    "completion",
)


def workflow_step_text(step):
    if not isinstance(step, dict):
        return ""

    return " ".join(
        [
            str(step.get("step_key") or ""),
            str(step.get("name") or ""),
            str(step.get("description") or ""),
            str(step.get("purpose") or ""),
        ]
    ).lower()


def workflow_kind_from_artifact(artifact):
    workflow_definition = artifact.get("workflow_definition")
    if isinstance(workflow_definition, dict):
        kind = str(workflow_definition.get("workflow_kind") or "").strip().lower()
        if kind:
            return kind

    kind = str(artifact.get("workflow_kind") or "").strip().lower()
    if kind:
        return kind

    return ""


def validate_approval_workflow_semantics(artifact):
    issues = []
    workflow_steps = artifact.get("workflow_steps") or []
    workflow_definition = artifact.get("workflow_definition")
    target_table = str(artifact.get("table") or "").strip().lower()

    approval_like = workflow_kind_from_artifact(artifact) == "approval"
    if not approval_like and isinstance(workflow_definition, dict):
        approval_like = any(
            key in workflow_definition
            for key in ("approval_threshold", "approval_group", "approval_subject")
        )

    if not approval_like:
        approval_like = any(
            str(step.get("table") or "").strip().lower() == "sysapproval_approver"
            for step in workflow_steps
            if isinstance(step, dict)
        )

    if not approval_like:
        return issues

    has_monitor_step = False
    has_finalizer_step = False

    for index, step in enumerate(workflow_steps, start=1):
        if not isinstance(step, dict):
            issues.append(f"step {index}: workflow steps must be objects")
            continue

        step_text = workflow_step_text(step)
        step_type = normalize_artifact_type(step.get("artifact_type"))
        step_table = str(step.get("table") or "").strip().lower()
        step_when = str(step.get("when") or "").strip().lower()

        if "helper" in step_text and step_type != "script_include":
            issues.append(f"step {index}: approval helper steps must use script_include")

        if any(term in step_text for term in _APPROVAL_MONITOR_TERMS):
            has_monitor_step = True
            if step_type != "business_rule":
                issues.append(f"step {index}: approval monitoring steps must use business_rule")
            if step_table != "sysapproval_approver":
                issues.append(f"step {index}: approval monitoring steps must target sysapproval_approver")
            if step_when and step_when != "after":
                issues.append(f"step {index}: approval monitoring steps must run after")

        if any(term in step_text for term in _APPROVAL_FINALIZER_TERMS):
            has_finalizer_step = True
            if step_type != "business_rule":
                issues.append(f"step {index}: approval finalization steps must use business_rule")
            if target_table and step_table != target_table:
                issues.append(f"step {index}: approval finalization steps must target {target_table}")
            if step_when and step_when != "after":
                issues.append(f"step {index}: approval finalization steps must run after")

        if step_table == "sysapproval_approver" and not any(term in step_text for term in _APPROVAL_MONITOR_TERMS):
            issues.append(f"step {index}: sysapproval_approver steps must monitor or evaluate approvals")

    if not has_monitor_step:
        issues.append("approval workflows must include a monitoring step on sysapproval_approver")

    if not has_finalizer_step:
        issues.append("approval workflows must include a source-record finalization step")

    return issues


def validate_script_content(script, require_script=False):
    if not isinstance(script, str):
        script = str(script or "")

    issues = []

    if require_script and not script.strip():
        issues.append("script is required")

    dangerous_patterns = [
        "while(true)",
        "gs.sleep",
        "deleteRecord(",
    ]

    for pattern in dangerous_patterns:
        if pattern in script:
            issues.append(pattern)

    return {
        "valid": len(issues) == 0,
        "issues": issues,
    }


def validate_workflow_artifact(artifact):
    issues = []

    if not artifact.get("name"):
        issues.append("workflow.name is required")

    if not artifact.get("table"):
        issues.append("workflow.table is required")

    workflow_definition = artifact.get("workflow_definition")
    workflow_steps = artifact.get("workflow_steps") or []

    if not workflow_definition and not artifact.get("description"):
        issues.append("workflow_definition or description is required")

    if not isinstance(workflow_steps, list):
        issues.append("workflow_steps must be a list")
    elif len(workflow_steps) < MIN_WORKFLOW_STEPS:
        issues.append(f"workflow_steps must contain at least {MIN_WORKFLOW_STEPS} deployable steps")
    else:
        orders = []
        for index, step in enumerate(workflow_steps, start=1):
            if isinstance(step, dict) and normalize_artifact_type(step.get("artifact_type")) == "workflow":
                issues.append(f"step {index}: nested workflow artifacts are not supported")
                continue

            if not isinstance(step, dict):
                issues.append(f"step {index}: workflow steps must be objects")
                continue

            if not step.get("name"):
                issues.append(f"step {index}: step name is required")

            order = step.get("order")
            if order is None:
                issues.append(f"step {index}: order is required")
            else:
                try:
                    orders.append(int(order))
                except Exception:
                    issues.append(f"step {index}: order must be an integer")

            step_validation = validate_artifact(step)

            if not step_validation.get("valid"):
                for issue in step_validation.get("issues", []):
                    issues.append(f"step {index}: {issue}")

        if orders and sorted(orders) != list(range(1, len(orders) + 1)):
            issues.append("workflow_steps order values must be sequential starting at 1 without gaps")

    issues.extend(validate_approval_workflow_semantics(artifact))

    if artifact.get("script"):
        script_validation = validate_script_content(artifact.get("script"), require_script=False)
        for issue in script_validation.get("issues", []):
            issues.append(f"workflow script: {issue}")

    placeholder_hits = [
        text
        for text in iter_strings(artifact)
        if any(token in text for token in PLACEHOLDER_TOKENS)
    ]

    if placeholder_hits:
        issues.append("workflow contains unresolved placeholder text; replace `YOUR_...` tokens before deployment")

    return {
        "valid": len(issues) == 0,
        "issues": issues,
    }


def validate_workflow_plan(plan):
    issues = []

    if not isinstance(plan, dict):
        return {
            "valid": False,
            "issues": ["workflow plan must be an object"],
        }

    if not plan.get("name"):
        issues.append("workflow_plan.name is required")

    if not plan.get("table"):
        issues.append("workflow_plan.table is required")

    workflow_definition = plan.get("workflow_definition")
    workflow_steps = plan.get("workflow_steps") or []

    if not workflow_definition and not plan.get("description"):
        issues.append("workflow_plan.workflow_definition or description is required")

    if not isinstance(workflow_steps, list):
        issues.append("workflow_plan.workflow_steps must be a list")
    elif len(workflow_steps) < MIN_WORKFLOW_STEPS:
        issues.append(f"workflow_plan.workflow_steps must contain at least {MIN_WORKFLOW_STEPS} steps")
    else:
        orders = []
        seen_keys = set()

        for index, step in enumerate(workflow_steps, start=1):
            if not isinstance(step, dict):
                issues.append(f"step {index}: workflow plan steps must be objects")
                continue

            step_type = normalize_artifact_type(step.get("artifact_type"))
            if step_type not in {"script_include", "business_rule", "client_script"}:
                issues.append(f"step {index}: invalid artifact_type for workflow plan step")

            step_key = str(step.get("step_key") or "").strip().lower()
            if not step_key:
                issues.append(f"step {index}: step_key is required")
            elif step_key in seen_keys:
                issues.append(f"step {index}: duplicate step_key {step_key}")
            else:
                seen_keys.add(step_key)

            if not step.get("name"):
                issues.append(f"step {index}: step name is required")

            if step_type in {"business_rule", "client_script"} and not step.get("table"):
                issues.append(f"step {index}: table is required for {step_type}")

            order = step.get("order")
            if order is None:
                issues.append(f"step {index}: order is required")
            else:
                try:
                    orders.append(int(order))
                except Exception:
                    issues.append(f"step {index}: order must be an integer")

            if step.get("script"):
                issues.append(f"step {index}: workflow plan steps must not include scripts")

        if orders and sorted(orders) != list(range(1, len(orders) + 1)):
            issues.append("workflow_plan step order values must be sequential starting at 1 without gaps")

    issues.extend(validate_approval_workflow_semantics(plan))

    return {
        "valid": len(issues) == 0,
        "issues": issues,
    }


def validate_artifact(artifact):
    if isinstance(artifact, dict):
        normalized = artifact
        artifact_type = normalize_artifact_type(artifact.get("artifact_type"))

        if artifact_type == "unknown":
            candidate = artifact.get("script")
            if isinstance(candidate, str):
                parsed = extract_json_blob(candidate)
                if isinstance(parsed, dict) and parsed.get("artifact_type"):
                    normalized = parsed
                    artifact_type = normalize_artifact_type(parsed.get("artifact_type"))
                else:
                    issues = [
                        "generated artifact is incomplete or malformed JSON; regenerate the workflow"
                    ]

                    return {
                        "valid": False,
                        "issues": issues,
                    }

        if artifact_type == "workflow_plan":
            return validate_workflow_plan(normalized)

        if artifact_type == "workflow":
            return validate_workflow_artifact(normalized)

        return validate_script_content(normalized.get("script"), require_script=True)

    return validate_script_content(artifact, require_script=True)


def validate_script(script):
    if isinstance(script, str):
        parsed = extract_json_blob(script)
        if isinstance(parsed, dict) and parsed.get("artifact_type"):
            return validate_artifact(parsed)

    if isinstance(script, dict):
        return validate_artifact(script)

    return validate_script_content(script, require_script=True)
