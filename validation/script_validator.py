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
        "workflow": "workflow",
        "classic workflow": "workflow",
    }

    return mapping.get(value, "unknown")


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

    if not isinstance(workflow_steps, list) or len(workflow_steps) == 0:
        issues.append("workflow_steps must contain at least one deployable step")
    else:
        for index, step in enumerate(workflow_steps, start=1):
            if isinstance(step, dict) and normalize_artifact_type(step.get("artifact_type")) == "workflow":
                issues.append(f"step {index}: nested workflow artifacts are not supported")
                continue

            step_validation = validate_artifact(step)

            if not step_validation.get("valid"):
                for issue in step_validation.get("issues", []):
                    issues.append(f"step {index}: {issue}")

    if artifact.get("script"):
        script_validation = validate_script_content(artifact.get("script"), require_script=False)
        for issue in script_validation.get("issues", []):
            issues.append(f"workflow script: {issue}")

    return {
        "valid": len(issues) == 0,
        "issues": issues,
    }


def validate_artifact(artifact):
    if isinstance(artifact, dict):
        artifact_type = normalize_artifact_type(artifact.get("artifact_type"))

        if artifact_type == "workflow":
            return validate_workflow_artifact(artifact)

        return validate_script_content(artifact.get("script"), require_script=True)

    return validate_script_content(artifact, require_script=True)


def validate_script(script):
    if isinstance(script, dict):
        return validate_artifact(script)

    return validate_script_content(script, require_script=True)
