def build_prompt(
    requirement: str,
    context: str,
    artifact_hint: str = "auto",
) -> str:
    """
    Builds structured prompt for LLM
    """

    artifact_hint = (artifact_hint or "auto").strip().lower().replace(" ", "_")
    if artifact_hint not in {"auto", "business_rule", "script_include", "client_script", "workflow"}:
        artifact_hint = "auto"

    return f"""
You are a ServiceNow expert developer.

Use the context below to generate a valid ServiceNow artifact.

### Context:
{context}

### Requirement:
{requirement}

### Artifact Hint:
{artifact_hint}

### Instructions:
- Identify the correct artifact type (business_rule / script_include / client_script / workflow)
- If the artifact hint is not auto, generate that exact artifact type.
- Infer the correct ServiceNow table from the requirement and context before writing the script.
- Do not ask the user for a table. The requirement text and context are the source of truth.
- If the requirement names a record type, choose the matching internal table name.
- Examples:
  - "business rule for incident" -> incident
  - "change request" -> change_request
  - "catalog item request" -> sc_req_item
  - "user" -> sys_user
- Generate clean, production-ready ServiceNow script
- Follow best practices (GlideRecord, try/catch, logging)
- For business_rule, client_script, and workflow, `table` is required and must be the internal table name.
- For script_include, `table` must be null.
- If the requirement mentions a record type, map it to the matching internal table name.
- Common mappings:
  - Incident -> incident
  - Change Request -> change_request
  - Problem -> problem
  - Task -> task
  - User -> sys_user
  - Group -> sys_user_group
  - Requested Item / Catalog Item Request -> sc_req_item
  - CMDB CI / Configuration Item -> cmdb_ci
- Include `when`, `insert`, and `update` when they are relevant to the artifact type.
- For client scripts, include the client script `type` when you can determine it.
- For workflow artifacts, build a workflow plan with deployable `workflow_steps`.
- Workflow steps must themselves be deployable ServiceNow artifacts.
- In v1, workflow steps should only use business_rule, script_include, or client_script.
- Workflow artifacts should include a short `description`, a `published` flag, and a `workflow_definition` object that explains the goal, trigger, and major decisions.
- For workflow artifacts, the top-level `script` can be null unless it is needed as a short summary.

### Output Format (STRICT JSON ONLY):
{{
  "artifact_type": "business_rule | script_include | client_script | workflow",
  "name": "Name of the artifact",
  "table": "Target table or null",
  "when": "before | after | null",
  "insert": true,
  "update": true,
  "order": 100,
  "type": "onSubmit | onLoad | onChange | null",
  "description": "Short summary or null",
  "published": false,
  "workflow_definition": null,
  "workflow_steps": [],
  "script": "FULL SCRIPT HERE OR NULL"
}}
"""


def build_table_inference_prompt(requirement: str, context: str, artifact_type: str, name: str = "", script: str = "") -> str:
    """
    Builds a narrow prompt that asks the LLM to infer the target table only.
    """

    return f"""
You infer ServiceNow table names.

Return STRICT JSON only:
{{
  "table": "internal_table_name_or_null"
}}

Rules:
- Infer the target table from the requirement and context.
- Do not ask the user a question.
- Use null only if the artifact type is script_include.
- The table must be the internal table name, not a label.
- For business_rule, client_script, and workflow, a non-null table is required.
- If the requirement points to a custom table, return the exact internal name.
- Examples:
  - incident -> incident
  - change request -> change_request
  - problem -> problem
  - task -> task
  - user -> sys_user
  - group -> sys_user_group
  - requested item -> sc_req_item
  - configuration item -> cmdb_ci

Artifact type:
{artifact_type}

Artifact name:
{name}

Requirement:
{requirement}

Context:
{context}

Generated script:
{script}
"""
