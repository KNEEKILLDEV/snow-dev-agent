import json


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
  - "external access request" -> sc_req_item
  - "access request" -> sc_req_item
  - "user" -> sys_user
- Generate clean, production-ready ServiceNow script
- Follow best practices (GlideRecord, try/catch, logging)
- Keep the response compact enough to fit in one model response. Prefer concise descriptions and concise script bodies.
- For workflows, keep step scripts as short as possible while remaining deployable, and avoid repeating the same logic across steps.
- For approval workflows, keep approval monitoring on `sysapproval_approver` and keep source-record finalization on the target table. Do not move approval-response handling back onto the request or change table.
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
- For workflow artifacts, build a workflow plan with at least 3 deployable `workflow_steps`.
- Workflow steps must themselves be deployable ServiceNow artifacts and must form a sequential bundle.
- The workflow should cover setup, execution, and outcome handling instead of stopping at a helper step.
- In v1, workflow steps should only use business_rule, script_include, or client_script.
- Workflow artifacts should include a short `description`, a `published` flag, and a `workflow_definition` object that explains the goal, trigger, and major decisions.
- For workflow artifacts, the top-level `script` can be null unless it is needed as a short summary.
- Do not include verbose commentary in the JSON; the artifact should be production-focused and concise.
- Do not invent CAB or change_request for access-request approvals unless the requirement explicitly says CAB or change request.
- Keep approval-group names and approval thresholds aligned to the requirement.
- Do not leave placeholder sys_ids such as `YOUR_CAB_APPROVAL_GROUP_SYS_ID`; resolve environment-specific references in the generated code when possible.

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


def build_workflow_expansion_prompt(
    requirement: str,
    context: str,
    workflow_name: str,
    workflow_definition,
    existing_steps,
    minimum_steps: int = 3,
) -> str:
    """
    Ask the LLM to expand an under-specified workflow into a full sequential plan.
    """

    workflow_definition_json = json.dumps(workflow_definition, ensure_ascii=True, indent=2, default=str)
    existing_steps_json = json.dumps(existing_steps, ensure_ascii=True, indent=2, default=str)

    return f"""
You expand ServiceNow workflow bundles into deployable sequential plans.

Return STRICT JSON only:
{{
  "workflow_steps": [
    {{
      "artifact_type": "business_rule | script_include | client_script",
      "name": "Name of the step",
      "table": "Target table or null",
      "when": "before | after | null",
      "insert": true,
      "update": true,
      "order": 1,
      "type": "onSubmit | onLoad | onChange | null",
      "description": "Short summary or null",
      "script": "FULL SCRIPT HERE OR NULL"
    }}
  ]
}}

Rules:
- Produce a complete sequential bundle with at least {minimum_steps} total deployable steps.
- Every step must be deployable and ordered from 1..n.
- Steps must build on each other: shared helper first, trigger/initiation next, outcome/finalization last.
- Workflow steps must only use business_rule, script_include, or client_script.
- Do not return a nested workflow artifact.
- Do not use placeholder identifiers such as `YOUR_...`; use concrete instance-aware values where possible.
- Keep each expanded step concise enough that the full response remains valid JSON.
- If the workflow is approval-oriented, include a step that creates or routes approvals and a later step that evaluates the result and updates the source record.
- For approval workflows, keep the monitoring step on `sysapproval_approver` and the finalization step on the source table. Only helper logic should be a script include.
- Do not invent CAB or change_request unless the requirement explicitly uses those terms.
- Keep approval-group names and approval thresholds aligned to the requirement.
- For external access or catalog access requests, use sc_req_item unless the requirement explicitly names another table.
- Preserve the intent of the existing steps below and add the missing sequential pieces needed to make the bundle work.
- The response must be valid JSON and nothing else.

Workflow name:
{workflow_name}

Workflow definition:
{workflow_definition_json}

Already present steps:
{existing_steps_json}

Requirement:
{requirement}

Context:
{context}
"""


def build_workflow_step_prompt(
    requirement: str,
    context: str,
    workflow_name: str,
    workflow_definition,
    current_step,
    prior_steps,
) -> str:
    """
    Ask the LLM to generate one deployable step at a time.
    """

    workflow_definition_json = json.dumps(workflow_definition, ensure_ascii=True, indent=2, default=str)
    current_step_json = json.dumps(current_step, ensure_ascii=True, indent=2, default=str)
    prior_steps_json = json.dumps(prior_steps, ensure_ascii=True, indent=2, default=str)

    return f"""
You generate exactly one deployable ServiceNow artifact for a staged workflow.

Return STRICT JSON only:
{{
  "artifact_type": "business_rule | script_include | client_script",
  "name": "Name of the artifact",
  "table": "Target table or null",
  "when": "before | after | null",
  "insert": true,
  "update": true,
  "order": 1,
  "type": "onSubmit | onLoad | onChange | null",
  "description": "Short summary or null",
  "published": false,
  "workflow_definition": null,
  "workflow_steps": [],
  "script": "FULL SCRIPT HERE"
}}

Rules:
- Generate only the current step, not the whole workflow.
- Match the current step's artifact_type, table, when, insert, update, type, and order.
- Keep the script concise and production-ready.
- Use Class.create() syntax for script includes.
- For business rules and client scripts, keep the code focused on this step only.
- If the current step is an approval monitoring step, keep it on `sysapproval_approver` and make the script act on approval rows while updating the parent source record via `current.sysapproval` or `current.document_id`.
- If the current step is a source-record finalization step, keep it on the source table and update the approval outcome from the approval records.
- Reference previous steps by name only when needed.
- Do not output markdown fences, explanations, or extra keys.
- Do not include placeholder sys_ids.
- Do not change the workflow table or invent CAB/change_request unless the current workflow_definition already says so.
- Keep access-request approval steps aligned to sc_req_item when the requirement is about external or catalog access requests.

Workflow name:
{workflow_name}

Workflow definition:
{workflow_definition_json}

Current step:
{current_step_json}

Prior deployed steps:
{prior_steps_json}

Requirement:
{requirement}

Context:
{context}
"""


def build_workflow_plan_prompt(
    requirement: str,
    context: str,
    workflow_kind: str,
    artifact_hint: str = "workflow",
) -> str:
    """
    Ask the LLM to create a compact workflow blueprint before step generation.
    """

    artifact_hint = (artifact_hint or "workflow").strip().lower().replace(" ", "_")
    workflow_kind = (workflow_kind or "generic").strip().lower()

    return f"""
You design a ServiceNow workflow blueprint, not the scripts.

Return STRICT JSON only:
{{
  "artifact_type": "workflow_plan",
  "name": "Name of the workflow",
  "table": "Internal table name",
  "description": "Short summary of the workflow",
  "published": false,
  "workflow_definition": {{
    "goal": "What the workflow should accomplish",
    "trigger": "The event or condition that starts it",
    "major_decisions": ["Decision 1", "Decision 2", "Decision 3"],
    "completion_criteria": ["Criterion 1", "Criterion 2"],
    "deployment_mode": "stepwise",
    "workflow_kind": "{workflow_kind}",
    "step_count": 3
  }},
  "workflow_steps": [
    {{
      "step_key": "unique_step_key",
      "artifact_type": "business_rule | script_include | client_script",
      "name": "Meaningful step name",
      "table": "Internal table name or null",
      "when": "before | after | null",
      "insert": true,
      "update": true,
      "type": "onSubmit | onLoad | onChange | null",
      "order": 1,
      "description": "Short summary of this step",
      "purpose": "Why this step exists",
      "depends_on": ["prior_step_key"],
      "script": null
    }}
  ],
  "script": null
}}

Rules:
- Create a distinct blueprint for this requirement. Do not reuse a fixed helper/gate/monitor/finalize skeleton unless that genuinely matches the business process.
- Derive the steps from the requirement and context. If the request is approval-oriented, include approval setup, response monitoring, and finalization. If it is onboarding, include intake, routing, completion, and any required notifications. If it is fulfillment, include intake, assignment/routing, execution, and closure.
- Use 3 to 7 deployable steps. Prefer the smallest number that fully covers the use case.
- Every step must be deployable and must be one of business_rule, script_include, or client_script.
- Keep each step to one responsibility. Avoid broad generic step names when the requirement is specific.
- For approval workflows, include a source-record setup step, an approval creation/routing step, an approval monitoring step on `sysapproval_approver`, and a source-record finalization step.
- Do not invent CAB or change_request for access-request approvals unless the requirement explicitly says CAB or change request.
- If the requirement names an approval group, keep that group name in the workflow definition and descriptions.
- For external access or catalog access requests, use sc_req_item unless the requirement explicitly names another table.
- Do not include scripts in the plan. Scripts are generated later, one step at a time.
- For business_rule, client_script, and workflow plans, table must be a concrete internal table name.
- For script_include, table must be null.
- For approval workflows, the monitoring step must be on `sysapproval_approver` and the finalization step must be on the source table.
- The workflow_definition and step names should reflect the actual process, not a boilerplate template.
- Do not use placeholder sys_ids or other environment placeholders.
- Keep the response compact enough to stay valid JSON.
- The plan should be specific to the requested use case, not a generic workflow template.
- Do not invent CAB or change_request unless the requirement explicitly uses those terms.
- Keep approval-group names and approval thresholds aligned to the requirement.

Artifact hint:
{artifact_hint}

Requirement:
{requirement}

Context:
{context}
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
  - external access request -> sc_req_item
  - access request -> sc_req_item
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
