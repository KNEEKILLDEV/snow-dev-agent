from agent.prompts import (
    build_prompt,
    build_table_inference_prompt,
    build_workflow_plan_prompt,
    build_workflow_expansion_prompt,
    build_workflow_step_prompt,
)
from agent.schema import Artifact
from config.settings import settings
from llm.router import ModelRouter
from pydantic import ValidationError
from validation.script_validator import validate_workflow_plan
from integration.servicenow_client import lookup_group_sys_id, truncate, write_debug_log
import json
import re
import os

router = ModelRouter(settings)
MIN_WORKFLOW_STEPS = 3


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

        "workflow plan": "workflow_plan",
        "workflow_plan": "workflow_plan",

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

    artifact_type = normalize_artifact_type(payload.get("artifact_type"))
    script = payload.get("script")

    if artifact_type == "script_include" and isinstance(script, str):
        class_name = sanitize_js_identifier(
            payload.get("type") or payload.get("name") or f"script_include_{step_index}",
            fallback=f"script_include_{step_index}",
        )
        canonical_script = canonicalize_script_include_script(script, class_name)

        if canonical_script != script:
            write_debug_log(
                "workflow_script_include_canonicalized",
                {
                    "requirement": requirement,
                    "provider": provider,
                    "step_name": payload.get("name"),
                    "class_name": class_name,
                },
            )
            script = canonical_script

    step = {
        "artifact_type": artifact_type,
        "name": payload.get("name") or f"workflow_step_{step_index}",
        "table": payload.get("table"),
        "when": payload.get("when"),
        "insert": payload.get("insert"),
        "update": payload.get("update"),
        "type": payload.get("type"),
        "order": payload.get("order") if payload.get("order") is not None else step_index,
        "description": payload.get("description"),
        "script": script,
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


def step_identity(step):
    if not isinstance(step, dict):
        return ("unknown", "", "")

    return (
        str(step.get("artifact_type") or "").strip().lower(),
        str(step.get("name") or "").strip().lower(),
        str(step.get("table") or "").strip().lower(),
    )


def dedupe_workflow_steps(steps):
    deduped = []
    seen = set()

    for step in steps or []:
        key = step_identity(step)

        if key in seen:
            continue

        seen.add(key)
        deduped.append(step)

    return deduped


def workflow_step_order(step):
    if not isinstance(step, dict):
        return 10**9

    order = step.get("order")
    if order is None:
        return 10**9

    try:
        return int(order)
    except Exception:
        return 10**9


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

_APPROVAL_GATE_TERMS = (
    "initialize",
    "initialise",
    "initiate",
    "intake",
    "prepare",
    "request",
    "create",
    "route",
    "trigger",
    "gate",
)


def workflow_step_text(step):
    if not isinstance(step, dict):
        return ""

    return _normalize_workflow_text(
        " ".join(
            [
                str(step.get("step_key") or ""),
                str(step.get("name") or ""),
                str(step.get("description") or ""),
                str(step.get("purpose") or ""),
            ]
        )
    ).lower()


def infer_workflow_step_role(step, workflow_kind="", target_table=None):
    step_text = workflow_step_text(step)
    workflow_kind = str(workflow_kind or "").strip().lower()

    if "helper" in step_text:
        return "helper"

    if workflow_kind == "approval":
        if any(term in step_text for term in _APPROVAL_MONITOR_TERMS):
            return "approval_monitor"

        if any(term in step_text for term in _APPROVAL_FINALIZER_TERMS):
            return "approval_finalizer"

        if any(term in step_text for term in _APPROVAL_GATE_TERMS):
            return "approval_gate"

    return "generic"


def enforce_workflow_step_contract(step, workflow_kind="", target_table=None):
    if not isinstance(step, dict):
        return step

    workflow_kind = str(workflow_kind or "").strip().lower()
    normalized = dict(step)
    role = infer_workflow_step_role(normalized, workflow_kind=workflow_kind, target_table=target_table)

    if role == "helper":
        normalized["artifact_type"] = "script_include"
        normalized["table"] = None
        normalized["when"] = None
        normalized["insert"] = False
        normalized["update"] = False
        normalized["type"] = None
        return normalized

    if workflow_kind != "approval":
        return normalized

    if role == "approval_monitor":
        normalized["artifact_type"] = "business_rule"
        normalized["table"] = "sysapproval_approver"
        normalized["when"] = "after"
        normalized["insert"] = False
        normalized["update"] = True
        normalized["type"] = None
        return normalized

    if role == "approval_finalizer":
        normalized["artifact_type"] = "business_rule"
        if target_table:
            normalized["table"] = target_table
        normalized["when"] = "after"
        normalized["insert"] = False
        normalized["update"] = True
        normalized["type"] = None
        return normalized

    if role == "approval_gate":
        normalized["artifact_type"] = "business_rule"
        if not normalized.get("table"):
            normalized["table"] = target_table
        if normalized.get("when") is None:
            if any(term in workflow_step_text(normalized) for term in ("initialize", "initialise", "intake", "prepare", "gate")):
                normalized["when"] = "before"
            else:
                normalized["when"] = "after"
        normalized["type"] = None

    return normalized


def workflow_step_contract_issues(candidate, expected):
    issues = []

    if not isinstance(candidate, dict) or not isinstance(expected, dict):
        return ["step contract must be a pair of objects"]

    def _coerce_boolish(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return bool(value)

    candidate_type = normalize_artifact_type(candidate.get("artifact_type"))
    expected_type = normalize_artifact_type(expected.get("artifact_type"))
    if candidate_type != expected_type:
        issues.append(
            f"artifact_type must be {expected_type or 'unknown'}"
        )

    candidate_table = str(candidate.get("table") or "").strip().lower()
    expected_table = str(expected.get("table") or "").strip().lower()
    if expected_table:
        if candidate_table != expected_table:
            issues.append(f"table must be {expected_table}")
    elif candidate_table:
        issues.append("table must be null")

    candidate_when = str(candidate.get("when") or "").strip().lower()
    expected_when = str(expected.get("when") or "").strip().lower()
    if expected_when:
        if candidate_when != expected_when:
            issues.append(f"when must be {expected_when}")
    elif candidate_when:
        issues.append("when must be null")

    candidate_type = str(candidate.get("type") or "").strip().lower()
    expected_type_value = str(expected.get("type") or "").strip().lower()
    if expected_type_value:
        if candidate_type != expected_type_value:
            issues.append(f"type must be {expected_type_value}")
    elif candidate_type:
        issues.append("type must be null")

    for field in ("insert", "update"):
        if _coerce_boolish(candidate.get(field)) != _coerce_boolish(expected.get(field)):
            issues.append(f"{field} must be {_coerce_boolish(expected.get(field))}")

    try:
        candidate_order = int(candidate.get("order"))
    except Exception:
        candidate_order = None

    try:
        expected_order = int(expected.get("order"))
    except Exception:
        expected_order = None

    if expected_order is not None and candidate_order != expected_order:
        issues.append(f"order must be {expected_order}")

    return issues


def sanitize_js_identifier(value, fallback="GeneratedScriptInclude"):
    text = re.sub(r"[^A-Za-z0-9_]", "_", str(value or "").strip())
    text = re.sub(r"_+", "_", text).strip("_")

    if not text:
        text = fallback

    if text[0].isdigit():
        text = f"_{text}"

    return text


_APPROVAL_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def _normalize_workflow_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def infer_approval_threshold(requirement):
    combined = _normalize_workflow_text(requirement).lower()

    patterns = [
        r"\bat least\s+(\d+)\s+approvals?\b",
        r"\bminimum(?:\s+of)?\s+(\d+)\s+approvals?\b",
        r"\b(\d+)\s+approvals?\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, combined)
        if match:
            try:
                return int(match.group(1))
            except Exception:
                continue

    word_match = re.search(
        r"\b(one|two|three|four|five|six|seven|eight|nine|ten)\s+approvals?\b",
        combined,
    )
    if word_match:
        return _APPROVAL_NUMBER_WORDS.get(word_match.group(1))

    quorum_match = re.search(r"\b(\d+)\s*[-/]?\s*of\s*(\d+)\b", combined)
    if quorum_match and "approval" in combined:
        try:
            return int(quorum_match.group(1))
        except Exception:
            return None

    return None


def infer_approval_group_name(requirement, context=""):
    combined = _normalize_workflow_text(f"{requirement} {context}")
    match = re.search(
        r"\b(?:from|by|for|within)\s+(?:the\s+)?(.+?)\s+group\b",
        combined,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()

    return None


def infer_workflow_subject(requirement, context, workflow_kind, table=None):
    combined = _normalize_workflow_text(f"{requirement} {context}").lower()
    table = str(table or "").strip().lower()
    workflow_kind = str(workflow_kind or "generic").strip().lower()

    if workflow_kind == "approval":
        if "cab" in combined and any(
            token in combined for token in ("5-of-8", "5 of 8", "five of eight")
        ):
            return "CAB 5-of-8"

        if "external access request" in combined:
            return "External Access Request"

        if "access request" in combined:
            return "Access Request"

        if "change request" in combined:
            return "Change Request"

        if table == "change_request":
            return "Change Request"

        if table == "sc_req_item":
            return "Access Request" if "access" in combined else "Request Item"

        return None

    if workflow_kind == "onboarding":
        if any(token in combined for token in ("new hire", "new-hire", "joiner")):
            return "New Hire"

        if "employee onboarding" in combined or "employee setup" in combined:
            return "Employee"

        if table == "sc_req_item":
            return "Onboarding Request"

        return None

    if workflow_kind == "fulfillment":
        if "external access request" in combined or "access request" in combined:
            return "Access Request"

        if any(token in combined for token in ("catalog item", "requested item")):
            return "Request Item"

        if table == "sc_req_item":
            return "Request Item"

        return None

    if table:
        return table.replace("_", " ").title()

    return None


def derive_workflow_trigger(requirement, context, workflow_kind, table, subject=None):
    combined = _normalize_workflow_text(f"{requirement} {context}").lower()
    subject = subject or infer_workflow_subject(requirement, context, workflow_kind, table)
    workflow_kind = str(workflow_kind or "generic").strip().lower()

    if workflow_kind == "approval":
        if "cab" in combined and any(
            token in combined for token in ("5-of-8", "5 of 8", "five of eight")
        ):
            return "The change request enters CAB review or approval is requested."

        if subject:
            return f"When the {subject.lower()} enters approval."

        return "When the source record enters approval."

    if workflow_kind == "onboarding":
        if subject == "New Hire":
            return "When a new hire onboarding request is submitted."

        if subject:
            return f"When the {subject.lower()} onboarding request is submitted."

        return "When the onboarding request is submitted."

    if workflow_kind == "fulfillment":
        if subject:
            return f"When the {subject.lower()} request is ready for fulfillment."

        return "When the request enters fulfillment."

    return "When the source record meets the workflow condition."


def workflow_plan_has_suspicious_approval_structure(plan, requirement, context, table):
    if not isinstance(plan, dict):
        return True

    combined = _normalize_workflow_text(f"{requirement} {context}").lower()
    plan_text = _normalize_workflow_text(json.dumps(plan, ensure_ascii=True, default=str)).lower()
    target_table = str(table or "").strip().lower()
    workflow_kind = infer_workflow_kind(requirement, context)

    if "cab" not in combined and "cab" in plan_text:
        return True

    if (
        target_table != "change_request"
        and "change request" not in combined
        and "change_request" in plan_text
    ):
        return True

    if workflow_kind == "approval":
        has_monitor_step = False

        for step in plan.get("workflow_steps") or []:
            if not isinstance(step, dict):
                return True

            role = infer_workflow_step_role(step, workflow_kind=workflow_kind, target_table=target_table)
            step_type = normalize_artifact_type(step.get("artifact_type"))
            step_table = str(step.get("table") or "").strip().lower()

            if role == "helper" and step_type != "script_include":
                return True

            if role == "approval_monitor":
                has_monitor_step = True
                if step_type != "business_rule" or step_table != "sysapproval_approver":
                    return True

            if role == "approval_finalizer" and target_table and step_table != target_table:
                return True

            if step_table == "sysapproval_approver" and role != "approval_monitor":
                return True

        if not has_monitor_step:
            return True

    allowed_tables = {target_table, "sysapproval_approver", ""}

    for step in plan.get("workflow_steps") or []:
        if not isinstance(step, dict):
            return True

        step_type = normalize_artifact_type(step.get("artifact_type"))
        if step_type not in {"script_include", "business_rule", "client_script"}:
            return True

        step_table = str(step.get("table") or "").strip().lower()
        if step_type in {"business_rule", "client_script"} and step_table and step_table not in allowed_tables:
            return True

    return False


def repair_malformed_json_response(
    response,
    requirement,
    context,
    provider,
    artifact_hint,
    extra_instructions="",
):
    repair_prompt = f"""
Your previous response was incomplete or invalid JSON.

Return STRICT JSON only and nothing else.
Do not use markdown fences.
Keep the response concise and fully valid.
{extra_instructions}

### Requirement:
{requirement}

### Context:
{context}

### Artifact Hint:
{artifact_hint}

### Previous Response:
{response}

### Instructions:
- Rebuild the full ServiceNow artifact from scratch.
- Preserve the intended artifact type and table.
- For workflow artifacts, include at least 3 deployable workflow_steps with sequential order values starting at 1.
- Keep descriptions concise to avoid truncation.
- Do not include placeholder sys_ids.
"""

    try:
        repair_response = router.generate(
            [
                {
                    "role": "system",
                    "content": "You repair malformed ServiceNow artifact JSON.",
                },
                {
                    "role": "user",
                    "content": repair_prompt,
                },
            ],
            provider=provider,
        )
    except Exception as exc:
        write_debug_log(
            "workflow_generation_repair_request_failed",
            {
                "requirement": requirement,
                "provider": provider,
                "artifact_hint": artifact_hint,
                "error": truncate(str(exc), 1000),
            },
        )
        return None

    repaired = extract_json(repair_response)

    write_debug_log(
        "workflow_generation_repair_response",
        {
            "requirement": requirement,
            "provider": provider,
            "artifact_hint": artifact_hint,
            "response": repair_response,
            "cleaned": repaired,
        },
    )

    return repaired


def canonicalize_script_include_script(script, class_name):
    if not isinstance(script, str):
        return script

    if re.search(r"\bClass\.create\s*\(", script):
        return script

    match = re.match(
        r"^\s*\(function[^{]*\{\s*return\s*\{\s*(.*)\s*\n\s*\};\s*\n\s*\}\)\(\);\s*$",
        script,
        re.DOTALL,
    )

    if not match:
        return script

    body = match.group(1).rstrip()

    return (
        f"var {class_name} = Class.create();\n"
        f"{class_name}.prototype = {{\n"
        f"{body}\n"
        f"}};\n"
    )


def replace_text_in_value(value, placeholder, replacement):
    if isinstance(value, str):
        return value.replace(placeholder, replacement)

    if isinstance(value, list):
        return [replace_text_in_value(item, placeholder, replacement) for item in value]

    if isinstance(value, dict):
        return {
            key: replace_text_in_value(item, placeholder, replacement)
            for key, item in value.items()
        }

    return value


def resolve_cab_helper_script(script, group_sys_id):
    if not isinstance(script, str) or not group_sys_id:
        return script

    script = script.replace(
        "this.cabGroupSysId = 'YOUR_CAB_APPROVAL_GROUP_SYS_ID';",
        f"this.cabGroupSysId = '{group_sys_id}';",
    )
    script = script.replace(
        f"this.cabGroupSysId = '{group_sys_id}';",
        f"this.cabGroupSysId = '{group_sys_id}';",
    )
    script = script.replace(
        "if (!this.cabGroupSysId || this.cabGroupSysId == 'YOUR_CAB_APPROVAL_GROUP_SYS_ID') {",
        "if (!this.cabGroupSysId) {",
    )
    script = script.replace(
        f"if (!this.cabGroupSysId || this.cabGroupSysId == '{group_sys_id}') {{",
        "if (!this.cabGroupSysId) {",
    )
    script = script.replace(
        "// Replace 'YOUR_CAB_APPROVAL_GROUP_SYS_ID' with the actual sys_id of your CAB Approval Group",
        "// CAB Approval Group sys_id resolved from the current instance.",
    )

    return script


def is_cab_quorum_workflow_requirement(requirement, context, artifact_hint):
    if normalize_artifact_hint(artifact_hint) not in {"auto", "workflow"}:
        return False

    combined = f"{requirement} {context}".lower()

    return (
        "cab" in combined
        and ("5-of-8" in combined or "5 of 8" in combined or "five of eight" in combined)
        and ("change_request" in combined or "change request" in combined)
    )


def build_cab_approval_workflow_artifact(requirement, context):
    return {
        "artifact_type": "workflow",
        "name": "CAB 5-of-8 Approval Workflow",
        "table": "change_request",
        "description": "Deployable CAB quorum bundle for change_request with setup, initiation, and approval monitoring.",
        "published": True,
        "workflow_definition": {
            "goal": "Approve a change_request only after 5 of 8 CAB members approve.",
            "trigger": "The change request enters CAB review or approval is requested.",
            "major_decisions": [
                "Resolve the CAB group from the current instance and collect up to 8 active members.",
                "Move CAB-eligible change requests into requested approval.",
                "Create one approval record per CAB member and watch each response.",
                "Finalize the change_request approval outcome after the quorum is met or lost.",
            ],
        },
        "workflow_steps": [
            {
                "artifact_type": "script_include",
                "name": "CABApprovalHelper",
                "table": None,
                "when": None,
                "insert": None,
                "update": None,
                "type": None,
                "order": 1,
                "description": "Helper for CAB member lookup, approval creation, and quorum evaluation.",
                "script": """var CABApprovalHelper = Class.create();
CABApprovalHelper.prototype = {
    initialize: function() {
        this.requiredApprovals = 5;
        this.cabMemberLimit = 8;
        this.groupNames = 'CAB Approval,eCAB Approval,Change Management';
    },

    resolveCabGroupSysId: function() {
        var group = new GlideRecord('sys_user_group');
        group.addQuery('name', 'IN', this.groupNames);
        group.query();

        if (group.next()) {
            return group.getUniqueValue();
        }

        gs.error('CABApprovalHelper: CAB group not found for names ' + this.groupNames);
        return '';
    },

    getCABMembers: function() {
        var members = [];
        var groupSysId = this.resolveCabGroupSysId();

        if (!groupSysId) {
            return members;
        }

        var member = new GlideRecord('sys_user_grmember');
        member.addQuery('group', groupSysId);
        member.addQuery('user.active', true);
        member.query();

        while (member.next() && members.length < this.cabMemberLimit) {
            members.push(member.getValue('user'));
        }

        if (members.length < this.cabMemberLimit) {
            gs.warn('CABApprovalHelper: found ' + members.length + ' CAB members; expected ' + this.cabMemberLimit);
        }

        return members;
    },

    requestCABApprovals: function(changeSysId) {
        if (!changeSysId) {
            return 0;
        }

        var created = 0;
        var approvers = this.getCABMembers();

        for (var i = 0; i < approvers.length; i++) {
            var approverId = approvers[i];
            var existing = new GlideRecord('sysapproval_approver');
            existing.addQuery('sysapproval', changeSysId);
            existing.addQuery('approver', approverId);
            existing.query();

            if (existing.next()) {
                continue;
            }

            var approval = new GlideRecord('sysapproval_approver');
            approval.initialize();
            approval.setValue('sysapproval', changeSysId);
            approval.setValue('approver', approverId);
            approval.setValue('state', 'requested');
            approval.insert();
            created++;
        }

        return created;
    },

    evaluateCABApproval: function(changeSysId) {
        if (!changeSysId) {
            return 'missing';
        }

        var approved = 0;
        var rejected = 0;
        var pending = 0;
        var change = new GlideRecord('change_request');

        if (!change.get(changeSysId)) {
            gs.error('CABApprovalHelper: change_request not found: ' + changeSysId);
            return 'missing';
        }

        var approval = new GlideRecord('sysapproval_approver');
        approval.addQuery('sysapproval', changeSysId);
        approval.query();

        while (approval.next()) {
            var state = approval.getValue('state');
            if (state == 'approved') {
                approved++;
            } else if (state == 'rejected') {
                rejected++;
            } else {
                pending++;
            }
        }

        if (approved >= this.requiredApprovals) {
            if (change.getValue('approval') != 'approved') {
                change.setValue('approval', 'approved');
                change.setValue('work_notes', 'CAB quorum reached: ' + approved + ' approvals.');
                change.update();
            }
            return 'approved';
        }

        if ((approved + pending) < this.requiredApprovals || (pending == 0 && approved < this.requiredApprovals)) {
            if (change.getValue('approval') != 'rejected') {
                change.setValue('approval', 'rejected');
                change.setValue('work_notes', 'CAB quorum not met: ' + approved + ' approvals.');
                change.update();
            }
            return 'rejected';
        }

        return 'requested';
    },

    type: 'CABApprovalHelper'
};""",
            },
            {
                "artifact_type": "business_rule",
                "name": "CABApprovalGate",
                "table": "change_request",
                "when": "before",
                "insert": True,
                "update": True,
                "type": None,
                "order": 2,
                "description": "Marks CAB-eligible change requests as approval requested when they enter review.",
                "script": """(function executeRule(current, previous) {
    var stateName = (current.getDisplayValue('state') || '').toLowerCase();
    var enteredCabStage = current.operation() == 'insert'
        ? (stateName == 'assess' || stateName == 'scheduled')
        : current.state.changes() && (stateName == 'assess' || stateName == 'scheduled');

    if (!enteredCabStage) {
        return;
    }

    if (current.getValue('approval') == 'approved' || current.getValue('approval') == 'rejected') {
        return;
    }

    if (current.getValue('approval') != 'requested') {
        current.setValue('approval', 'requested');
    }
})(current, previous);""",
            },
            {
                "artifact_type": "business_rule",
                "name": "CABApprovalTrigger",
                "table": "change_request",
                "when": "after",
                "insert": False,
                "update": True,
                "type": None,
                "order": 3,
                "description": "Creates CAB approval rows when the change request enters requested approval.",
                "script": """(function executeRule(current, previous) {
    if (!current.approval.changesTo('requested')) {
        return;
    }

    var helper = new CABApprovalHelper();
    var created = helper.requestCABApprovals(current.getUniqueValue());

    if (!created) {
        gs.warn('CABApprovalTrigger: no CAB approvals created for ' + current.getUniqueValue());
    }
})(current, previous);""",
            },
            {
                "artifact_type": "business_rule",
                "name": "MonitorCABApprovals",
                "table": "sysapproval_approver",
                "when": "after",
                "insert": False,
                "update": True,
                "type": None,
                "order": 4,
                "description": "Re-evaluates the CAB quorum whenever an approval state changes.",
                "script": """(function executeRule(current, previous) {
    if (!current.state.changes()) {
        return;
    }

    if (current.sysapproval.nil()) {
        return;
    }

    var target = current.sysapproval.getRefRecord();
    if (!target || !target.isValidRecord() || target.getTableName() != 'change_request') {
        return;
    }

    new CABApprovalHelper().evaluateCABApproval(target.getUniqueValue());
})(current, previous);""",
            },
        ],
        "script": None,
    }


def resolve_workflow_placeholders(workflow_artifact, requirement, context, provider):
    if not isinstance(workflow_artifact, dict):
        return workflow_artifact

    placeholder = "YOUR_CAB_APPROVAL_GROUP_SYS_ID"
    artifact_text = json.dumps(workflow_artifact, ensure_ascii=True, default=str)

    if placeholder not in artifact_text:
        return workflow_artifact

    group_sys_id = lookup_group_sys_id(
        "CAB Approval",
        "eCAB Approval",
        "Change Management",
    )

    if not group_sys_id:
        write_debug_log(
            "workflow_placeholder_unresolved",
            {
                "requirement": requirement,
                "provider": provider,
                "artifact_name": workflow_artifact.get("name"),
                "placeholder": placeholder,
            },
        )
        return workflow_artifact

    write_debug_log(
        "workflow_placeholder_resolved",
        {
            "requirement": requirement,
            "provider": provider,
            "artifact_name": workflow_artifact.get("name"),
            "placeholder": placeholder,
            "replacement": group_sys_id,
        },
    )

    normalized = replace_text_in_value(workflow_artifact, placeholder, group_sys_id)

    for step in normalized.get("workflow_steps") or []:
        if isinstance(step, dict) and isinstance(step.get("script"), str):
            step["script"] = resolve_cab_helper_script(step["script"], group_sys_id)

    return normalized


def expand_workflow_steps(requirement, context, provider, workflow_artifact, existing_steps, minimum_steps=MIN_WORKFLOW_STEPS):
    if len(existing_steps) >= minimum_steps:
        return existing_steps

    prompt_text = build_workflow_expansion_prompt(
        requirement=requirement,
        context=context,
        workflow_name=workflow_artifact.get("name") or "generated_workflow",
        workflow_definition=workflow_artifact.get("workflow_definition") or {},
        existing_steps=existing_steps,
        minimum_steps=minimum_steps,
    )

    write_debug_log(
        "workflow_step_expansion_start",
        {
            "requirement": requirement,
            "provider": provider,
            "workflow_name": workflow_artifact.get("name"),
            "existing_step_count": len(existing_steps),
            "minimum_steps": minimum_steps,
            "prompt": prompt_text,
            "request_timeout_seconds": settings.LLM_REQUEST_TIMEOUT_SECONDS,
        },
    )

    response = router.generate(
        [
            {
                "role": "system",
                "content": "You are a ServiceNow workflow planning assistant.",
            },
            {
                "role": "user",
                "content": prompt_text,
            },
        ],
        provider=provider,
    )

    cleaned = extract_json(response)

    write_debug_log(
        "workflow_step_expansion_raw_response",
        {
            "requirement": requirement,
            "provider": provider,
            "workflow_name": workflow_artifact.get("name"),
            "response": response,
            "cleaned": cleaned,
        },
    )

    try:
        payload = json.loads(cleaned)
        raw_steps = payload.get("workflow_steps") or payload.get("steps") or []
        expanded_steps = []
        workflow_definition = workflow_artifact.get("workflow_definition") or {}
        workflow_kind = str(workflow_definition.get("workflow_kind") or infer_workflow_kind(requirement, context)).strip().lower()
        target_table = (
            workflow_artifact.get("table")
            or workflow_artifact.get("requested_table")
            or guess_table_from_text(
                requirement,
                context,
                workflow_artifact.get("name"),
                workflow_artifact.get("description"),
            )
        )

        for index, raw_step in enumerate(raw_steps, start=len(existing_steps) + 1):
            if isinstance(raw_step, dict) and normalize_artifact_type(raw_step.get("artifact_type")) == "workflow":
                raise ValueError("Nested workflow artifacts are not supported")

            expanded_step = build_deployable_step(
                raw_step,
                requirement=requirement,
                context=context,
                provider=provider,
                fallback_table=target_table,
                step_index=index,
            )
            expanded_steps.append(
                enforce_workflow_step_contract(
                    expanded_step,
                    workflow_kind=workflow_kind,
                    target_table=target_table,
                )
            )

        combined_steps = dedupe_workflow_steps([*existing_steps, *expanded_steps])
        combined_steps.sort(key=workflow_step_order)

        for index, step in enumerate(combined_steps, start=1):
            step["order"] = index

        write_debug_log(
            "workflow_step_expansion_result",
            {
                "requirement": requirement,
                "provider": provider,
                "workflow_name": workflow_artifact.get("name"),
                "step_count": len(combined_steps),
                "steps": combined_steps,
            },
        )

        return combined_steps
    except Exception as exc:
        write_debug_log(
            "workflow_step_expansion_error",
            {
                "requirement": requirement,
                "provider": provider,
                "workflow_name": workflow_artifact.get("name"),
                "error": truncate(str(exc), 2000),
            },
        )

        return existing_steps


def build_workflow_artifact(data, requirement, context, provider):
    if not isinstance(data, dict):
        data = {}

    normalized = dict(data)
    normalized["artifact_type"] = "workflow"
    workflow_kind = infer_workflow_kind(requirement, context)
    target_table = (
        normalized.get("table")
        or guess_table_from_text(requirement, context, normalized.get("name"), normalized.get("description"))
        or (infer_missing_table(
            requirement=requirement,
            context=context,
            provider=provider,
            artifact_type="workflow",
            artifact_name=normalized.get("name", ""),
            script=normalized.get("script", ""),
        ) if workflow_kind == "approval" else None)
    )

    raw_steps = normalized.get("workflow_steps") or []
    workflow_steps = []

    for index, raw_step in enumerate(raw_steps, start=1):
        if isinstance(raw_step, dict) and normalize_artifact_type(raw_step.get("artifact_type")) == "workflow":
            raise ValueError("Nested workflow steps are not supported yet")

        workflow_steps.append(
            enforce_workflow_step_contract(
                build_deployable_step(
                    raw_step,
                    requirement=requirement,
                    context=context,
                    provider=provider,
                    fallback_table=target_table or normalized.get("table"),
                    step_index=index,
                ),
                workflow_kind=workflow_kind,
                target_table=target_table,
            )
        )

    workflow_steps = dedupe_workflow_steps(workflow_steps)

    if not workflow_steps and (
        normalized.get("script")
        or normalized.get("when") is not None
        or normalized.get("insert") is not None
        or normalized.get("update") is not None
        or normalized.get("type")
    ):
        workflow_steps.append(
            enforce_workflow_step_contract(
                build_deployable_step(
                    normalized,
                    requirement=requirement,
                    context=context,
                    provider=provider,
                    fallback_table=target_table or normalized.get("table"),
                    step_index=1,
                ),
                workflow_kind=workflow_kind,
                target_table=target_table,
            )
        )
        normalized["script"] = None

    if len(workflow_steps) < MIN_WORKFLOW_STEPS:
        workflow_steps = expand_workflow_steps(
            requirement=requirement,
            context=context,
            provider=provider,
            workflow_artifact=normalized,
            existing_steps=workflow_steps,
            minimum_steps=MIN_WORKFLOW_STEPS,
        )

    workflow_steps = dedupe_workflow_steps(workflow_steps)
    workflow_steps.sort(key=workflow_step_order)

    for index, step in enumerate(workflow_steps, start=1):
        step["order"] = index

    if len(workflow_steps) < MIN_WORKFLOW_STEPS:
        write_debug_log(
            "workflow_step_expansion_insufficient",
            {
                "requirement": requirement,
                "provider": provider,
                "workflow_name": normalized.get("name"),
                "step_count": len(workflow_steps),
                "minimum_steps": MIN_WORKFLOW_STEPS,
            },
        )
        raise ValueError(
            f"Workflow artifacts require at least {MIN_WORKFLOW_STEPS} deployable workflow steps."
        )

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

    target_table = normalized.get("table") or target_table
    workflow_steps = [
        enforce_workflow_step_contract(
            step,
            workflow_kind=workflow_kind,
            target_table=target_table,
        )
        for step in workflow_steps
    ]

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
    elif isinstance(workflow_definition, dict):
        workflow_definition = dict(workflow_definition)
        workflow_definition["step_count"] = len(workflow_steps)
        workflow_definition["deployment_mode"] = "sequential"

    normalized["workflow_definition"] = workflow_definition
    normalized["workflow_steps"] = workflow_steps
    normalized["script"] = None

    normalized = resolve_workflow_placeholders(
        workflow_artifact=normalized,
        requirement=requirement,
        context=context,
        provider=provider,
    )

    return normalized


def generate_workflow_step_artifact(
    requirement,
    context,
    provider,
    workflow_plan,
    step_index,
    prior_steps=None,
):
    if not isinstance(workflow_plan, dict):
        raise ValueError("workflow_plan must be a dictionary")

    workflow_steps = workflow_plan.get("workflow_steps") or []
    if step_index < 0 or step_index >= len(workflow_steps):
        raise IndexError("workflow step index out of range")

    current_step = dict(workflow_steps[step_index] or {})
    prior_steps = prior_steps or []

    prompt_text = build_workflow_step_prompt(
        requirement=requirement,
        context=context,
        workflow_name=workflow_plan.get("name") or "generated_workflow",
        workflow_definition=workflow_plan.get("workflow_definition") or {},
        current_step=current_step,
        prior_steps=prior_steps,
    )

    write_debug_log(
        "workflow_step_generation_start",
        {
            "requirement": requirement,
            "provider": provider,
            "workflow_name": workflow_plan.get("name"),
            "step_index": step_index + 1,
            "current_step": current_step,
            "prior_step_count": len(prior_steps),
            "prompt": prompt_text,
            "request_timeout_seconds": settings.LLM_REQUEST_TIMEOUT_SECONDS,
        },
    )

    response = router.generate(
        [
            {
                "role": "system",
                "content": "You generate one deployable ServiceNow workflow step at a time.",
            },
            {
                "role": "user",
                "content": prompt_text,
            },
        ],
        provider=provider,
    )

    cleaned = extract_json(response)

    write_debug_log(
        "workflow_step_generation_raw_response",
        {
            "requirement": requirement,
            "provider": provider,
            "workflow_name": workflow_plan.get("name"),
            "step_index": step_index + 1,
            "response": response,
            "cleaned": cleaned,
        },
    )

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        repaired = repair_malformed_json_response(
            response=response,
            requirement=requirement,
            context=context,
            provider=provider,
            artifact_hint=current_step.get("artifact_type") or "workflow_step",
            extra_instructions=(
                "Rebuild only the current workflow step as a single deployable artifact. "
                "Do not include workflow_steps or a workflow wrapper. "
                f"Current step JSON: {json.dumps(current_step, ensure_ascii=True, default=str)}"
            ),
        )

        if not repaired:
            raise

        cleaned = repaired
        data = json.loads(cleaned)

    contract_issues = workflow_step_contract_issues(data, current_step)
    if contract_issues:
        repaired = repair_malformed_json_response(
            response=response,
            requirement=requirement,
            context=context,
            provider=provider,
            artifact_hint=current_step.get("artifact_type") or "workflow_step",
            extra_instructions=(
                "The previous step response did not match the current workflow step contract. "
                "Regenerate the step so the contract fields match exactly. "
                f"Current step contract: {json.dumps(current_step, ensure_ascii=True, default=str)} "
                "For approval monitoring steps, use sysapproval_approver and update the parent source record via current.sysapproval or current.document_id. "
                "Do not change the contract fields."
            ),
        )

        if repaired:
            cleaned = repaired
            data = json.loads(cleaned)
            contract_issues = workflow_step_contract_issues(data, current_step)

        if contract_issues:
            raise ValueError(
                "Generated workflow step does not match the planned step contract: "
                + "; ".join(contract_issues)
            )

    normalized = build_deployable_step(
        data,
        requirement=requirement,
        context=context,
        provider=provider,
        fallback_table=current_step.get("table") or workflow_plan.get("table"),
        step_index=current_step.get("order") or step_index + 1,
    )

    step_type = current_step.get("artifact_type") or normalized.get("artifact_type")
    normalized["artifact_type"] = step_type
    normalized["name"] = normalized.get("name") or current_step.get("name") or f"workflow_step_{step_index + 1}"
    normalized["when"] = current_step.get("when")
    normalized["insert"] = current_step.get("insert")
    normalized["update"] = current_step.get("update")
    normalized["type"] = current_step.get("type")
    normalized["order"] = current_step.get("order") or (step_index + 1)
    normalized["description"] = normalized.get("description") or current_step.get("description")
    if artifact_requires_table(step_type):
        normalized["table"] = current_step.get("table") or normalized.get("table") or workflow_plan.get("table")
    else:
        normalized["table"] = None
    normalized["workflow_definition"] = None
    normalized["workflow_steps"] = []

    artifact = Artifact.model_validate(normalized)

    if artifact.artifact_type == "workflow":
        raise ValueError("Nested workflow artifacts are not supported")

    if artifact_requires_table(artifact.artifact_type) and not artifact.table:
        raise ValueError("Generated workflow step is missing a target table")

    if artifact.artifact_type in {"business_rule", "script_include", "client_script"} and not artifact.script:
        raise ValueError("Generated workflow step is missing script text")

    write_debug_log(
        "workflow_step_generation_final_artifact",
        {
            "requirement": requirement,
            "provider": provider,
            "workflow_name": workflow_plan.get("name"),
            "step_index": step_index + 1,
            "artifact": artifact.model_dump(),
        },
    )

    return artifact.model_dump()


def guess_table_from_text(*texts):
    combined = " ".join([str(text or "") for text in texts]).lower()

    patterns = [
        (r"\bexternal access request(s)?\b", "sc_req_item"),
        (r"\baccess request(s)?\b", "sc_req_item"),
        (r"\brequest(s)? for access\b", "sc_req_item"),
        (r"\baccess provisioning\b", "sc_req_item"),
        (r"\bincident(s)?\b", "incident"),
        (r"\bproblem(s)?\b", "problem"),
        (r"\bchange[_ ]request(s)?\b", "change_request"),
        (r"\bnew hire(s)?\b", "sc_req_item"),
        (r"\bonboarding\b", "sc_req_item"),
        (r"\bjoiner(s)?\b", "sc_req_item"),
        (r"\brequest(ed)? item(s)?\b", "sc_req_item"),
        (r"\bcatalog item request(s)?\b", "sc_req_item"),
        (r"\bsc[_ ]req[_ ]item\b", "sc_req_item"),
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


def infer_workflow_kind(requirement, context):
    combined = f"{requirement} {context}".lower()

    if any(
        token in combined
        for token in (
            "cab",
            "approval",
            "quorum",
            "sign-off",
            "sign off",
            "approve",
        )
    ):
        return "approval"

    if any(
        token in combined
        for token in (
            "onboarding",
            "new hire",
            "new-hire",
            "joiner",
            "employee setup",
            "employee onboarding",
        )
    ):
        return "onboarding"

    if any(
        token in combined
        for token in (
            "fulfillment",
            "provisioning",
            "provision",
            "access request",
            "request item",
            "catalog item",
            "sc_req_item",
        )
    ):
        return "fulfillment"

    return "generic"


def derive_workflow_name(requirement, table, workflow_kind):
    combined = str(requirement or "").lower()
    table = str(table or "").strip().lower()
    workflow_kind = str(workflow_kind or "generic").strip().lower()

    if workflow_kind == "approval":
        if "cab" in combined and any(
            token in combined for token in ("5-of-8", "5 of 8", "five of eight")
        ):
            return "CAB 5-of-8 Approval Workflow"

        subject = infer_workflow_subject(requirement, "", workflow_kind, table)
        if subject:
            return f"{subject} Approval Workflow"

        if table:
            return f"{table.replace('_', ' ').title()} Approval Workflow"

        return "Approval Workflow"

    if workflow_kind == "onboarding":
        subject = infer_workflow_subject(requirement, "", workflow_kind, table)
        if subject:
            return f"{subject} Onboarding Workflow"

        return "Onboarding Workflow"

    if workflow_kind == "fulfillment":
        subject = infer_workflow_subject(requirement, "", workflow_kind, table)
        if subject:
            return f"{subject} Fulfillment Workflow"

        return "Fulfillment Workflow"

    if table == "change_request":
        return "Change Request Workflow"

    if table == "sc_req_item":
        return "Catalog Request Workflow"

    if table:
        return f"{table.replace('_', ' ').title()} Workflow"

    return "Generated Workflow"


def build_workflow_plan_step(
    *,
    step_key,
    name,
    artifact_type,
    table=None,
    when=None,
    insert=False,
    update=False,
    type=None,
    description=None,
    purpose=None,
    depends_on=None,
    order=1,
):
    return {
        "step_key": step_key,
        "name": name,
        "artifact_type": artifact_type,
        "table": table,
        "when": when,
        "insert": insert,
        "update": update,
        "type": type,
        "order": order,
        "description": description,
        "purpose": purpose,
        "depends_on": depends_on or [],
        "script": None,
    }


def build_generic_approval_workflow_plan(requirement, context, table, workflow_name, artifact_hint="workflow"):
    approval_threshold = infer_approval_threshold(requirement)
    approval_group = infer_approval_group_name(requirement, context)
    subject = infer_workflow_subject(requirement, context, "approval", table)
    target_label = subject.lower() if subject else "the source record"
    group_label = approval_group or "the designated approval group"
    quorum_label = f"{approval_threshold} approvals" if approval_threshold else "the required approval quorum"

    steps = [
        build_workflow_plan_step(
            step_key="helper",
            name="ApprovalHelper",
            artifact_type="script_include",
            table=None,
            order=1,
            description=f"Shared helper for {group_label} lookup and quorum evaluation.",
            purpose="Encapsulate reusable approval helper logic.",
            depends_on=[],
        ),
        build_workflow_plan_step(
            step_key="gate",
            name="ApprovalGate",
            artifact_type="business_rule",
            table=table,
            when="before",
            insert=True,
            update=True,
            order=2,
            description="Marks the record for approval processing without creating duplicate approval requests.",
            purpose=f"Enter approval tracking for {target_label}.",
            depends_on=["helper"],
        ),
        build_workflow_plan_step(
            step_key="monitor",
            name="ApprovalMonitor",
            artifact_type="business_rule",
            table="sysapproval_approver",
            when="after",
            update=True,
            order=3,
            description="Re-evaluates the approval quorum whenever an approver responds.",
            purpose=f"Watch approval responses and compare them to the {quorum_label}.",
            depends_on=["helper", "gate"],
        ),
        build_workflow_plan_step(
            step_key="finalize",
            name="ApprovalFinalizer",
            artifact_type="business_rule",
            table=table,
            when="after",
            update=True,
            order=4,
            description="Finalizes the approval outcome and prevents reopening completed records.",
            purpose="Set the final approval state and close the workflow cleanly.",
            depends_on=["helper", "gate", "monitor"],
        ),
    ]

    major_decisions = [
        f"Resolve {group_label} and collect active approvers.",
        "Create approval records only once when the request enters review.",
        f"Re-evaluate the quorum on every approval response against {quorum_label}.",
        f"Finalize the {target_label} once approval succeeds or becomes impossible.",
    ]

    workflow_definition = {
        "goal": requirement,
        "trigger": derive_workflow_trigger(
            requirement=requirement,
            context=context,
            workflow_kind="approval",
            table=table,
            subject=subject,
        ),
        "major_decisions": major_decisions,
        "completion_criteria": [
            f"At least {approval_threshold} approvals are recorded." if approval_threshold else "The required approval quorum is recorded.",
            f"The {target_label} is marked Approved.",
            "The final approval state is written back to the source record.",
        ],
        "deployment_mode": "stepwise",
        "workflow_kind": "approval",
        "step_count": len(steps),
    }

    if approval_threshold:
        workflow_definition["approval_threshold"] = approval_threshold

    if approval_group:
        workflow_definition["approval_group"] = approval_group

    if subject:
        workflow_definition["approval_subject"] = subject

    return {
        "artifact_type": "workflow_plan",
        "name": workflow_name,
        "table": table,
        "description": f"Stepwise workflow plan for {workflow_name}.",
        "published": False,
        "workflow_definition": workflow_definition,
        "workflow_steps": steps,
        "script": None,
        "requested_artifact_type": normalize_artifact_hint(artifact_hint),
    }


def build_workflow_plan(requirement, context, artifact_hint="auto", provider=None):
    provider = provider or settings.DEFAULT_PROVIDER

    generated_plan = generate_workflow_plan_artifact(
        requirement=requirement,
        context=context,
        provider=provider,
        artifact_hint=artifact_hint,
    )
    if generated_plan:
        return generated_plan

    workflow_kind = infer_workflow_kind(requirement, context)
    table = guess_table_from_text(requirement, context)

    if not table:
        if workflow_kind == "approval":
            table = "change_request"
        elif workflow_kind in {"onboarding", "fulfillment"}:
            table = "sc_req_item"

    if not table and workflow_kind == "generic":
        table = infer_missing_table(
            requirement=requirement,
            context=context,
            provider=settings.DEFAULT_PROVIDER,
            artifact_type="workflow",
            artifact_name="",
            script="",
        )

    workflow_name = derive_workflow_name(requirement, table, workflow_kind)
    requirement_text = f"{requirement} {context}".lower()

    if workflow_kind == "approval":
        if not is_cab_quorum_workflow_requirement(requirement, context, artifact_hint):
            if not table:
                table = "change_request"

            return build_generic_approval_workflow_plan(
                requirement=requirement,
                context=context,
                table=table,
                workflow_name=workflow_name,
                artifact_hint=artifact_hint,
            )

        if not table:
            table = "change_request"

        steps = [
            build_workflow_plan_step(
                step_key="helper",
                name="CABApprovalHelper",
                artifact_type="script_include",
                table=None,
                order=1,
                description="Shared helper for CAB member lookup and quorum evaluation.",
                purpose="Encapsulate reusable approval helper logic.",
                depends_on=[],
            ),
            build_workflow_plan_step(
                step_key="gate",
                name="CABApprovalGate",
                artifact_type="business_rule",
                table=table,
                when="before",
                insert=True,
                update=True,
                order=2,
                description="Marks CAB-eligible changes as approval requested.",
                purpose="Enter the CAB approval stage without duplicating approvals.",
                depends_on=["helper"],
            ),
            build_workflow_plan_step(
                step_key="monitor",
                name="MonitorCABApprovals",
                artifact_type="business_rule",
                table="sysapproval_approver",
                when="after",
                update=True,
                order=3,
                description="Re-evaluates the approval quorum whenever an approver responds.",
                purpose="Watch approval state changes and recalculate quorum.",
                depends_on=["helper", "gate"],
            ),
            build_workflow_plan_step(
                step_key="finalize",
                name="CABApprovalFinalizer",
                artifact_type="business_rule",
                table=table,
                when="after",
                update=True,
                order=4,
                description="Finalizes the change request after the quorum is reached or lost.",
                purpose="Set the final approval state and prevent reopening completed records.",
                depends_on=["helper", "gate", "monitor"],
            ),
        ]
        major_decisions = [
            "Resolve the CAB group from the instance and collect up to 8 active members.",
            "Create approvals only once when the change enters CAB review.",
            "Re-evaluate the quorum on every approval response.",
            "Finalize the change_request after 5 approvals or when approval becomes impossible.",
        ]

    elif workflow_kind == "onboarding":
        if not table:
            table = "sc_req_item"

        steps = [
            build_workflow_plan_step(
                step_key="helper",
                name="NewHireOnboardingHelper",
                artifact_type="script_include",
                table=None,
                order=1,
                description="Shared helper for onboarding lookups and task routing.",
                purpose="Keep onboarding helper logic reusable.",
                depends_on=[],
            ),
            build_workflow_plan_step(
                step_key="intake",
                name="NewHireOnboardingGate",
                artifact_type="business_rule",
                table=table,
                when="before",
                insert=True,
                update=True,
                order=2,
                description="Validates onboarding requests and normalizes the request item.",
                purpose="Prepare the request item for downstream onboarding tasks.",
                depends_on=["helper"],
            ),
            build_workflow_plan_step(
                step_key="route",
                name="NewHireTaskRouter",
                artifact_type="business_rule",
                table="sc_task",
                when="after",
                insert=True,
                update=True,
                order=3,
                description="Routes onboarding tasks to the correct fulfillment groups.",
                purpose="Fan out the onboarding work once the request is ready.",
                depends_on=["helper", "intake"],
            ),
            build_workflow_plan_step(
                step_key="complete",
                name="NewHireCompletionMonitor",
                artifact_type="business_rule",
                table=table,
                when="after",
                update=True,
                order=4,
                description="Detects completion and closes the onboarding request cleanly.",
                purpose="Mark the onboarding request complete when tasks finish.",
                depends_on=["helper", "intake", "route"],
            ),
        ]
        major_decisions = [
            "Validate incoming onboarding data on the request item.",
            "Create or route tasks for the required fulfillment groups.",
            "Watch task completion and close the request when work is done.",
            "Keep helper logic in a shared script include so step scripts stay small.",
        ]

    else:
        if not table:
            table = guess_table_from_text(requirement_text) or "task"

        steps = [
            build_workflow_plan_step(
                step_key="helper",
                name="WorkflowHelper",
                artifact_type="script_include",
                table=None,
                order=1,
                description="Shared helper for workflow-specific lookups and reusable logic.",
                purpose="Keep repeated code in one deployable helper.",
                depends_on=[],
            ),
            build_workflow_plan_step(
                step_key="trigger",
                name="WorkflowTrigger",
                artifact_type="business_rule",
                table=table,
                when="before",
                insert=True,
                update=True,
                order=2,
                description="Starts the workflow when the source record enters the target condition.",
                purpose="Initialize workflow state on the source table.",
                depends_on=["helper"],
            ),
            build_workflow_plan_step(
                step_key="monitor",
                name="WorkflowMonitor",
                artifact_type="business_rule",
                table=table,
                when="after",
                update=True,
                order=3,
                description="Monitors state changes and determines whether the workflow can move forward.",
                purpose="Track progress and evaluate completion conditions.",
                depends_on=["helper", "trigger"],
            ),
            build_workflow_plan_step(
                step_key="finalize",
                name="WorkflowFinalizer",
                artifact_type="business_rule",
                table=table,
                when="after",
                update=True,
                order=4,
                description="Finalizes the workflow outcome and writes the final state back.",
                purpose="Complete the workflow cleanly and prevent reopening.",
                depends_on=["helper", "trigger", "monitor"],
            ),
        ]
        major_decisions = [
            "Prepare reusable helper logic first.",
            "Trigger the process from the source table.",
            "Monitor the workflow as records change.",
            "Finalize the outcome once the required conditions are met.",
        ]

    workflow_definition = {
        "goal": requirement,
        "trigger": context or "Detected from the requirement and source table.",
        "major_decisions": major_decisions,
        "deployment_mode": "stepwise",
        "workflow_kind": workflow_kind,
        "step_count": len(steps),
    }

    if "new hire" in requirement_text or "onboarding" in requirement_text:
        workflow_definition["completion_criteria"] = [
            "Required onboarding tasks are created and routed.",
            "All onboarding work items are completed.",
            "The request item is marked complete.",
        ]
    elif workflow_kind == "approval":
        workflow_definition["completion_criteria"] = [
            "Five approvals are recorded.",
            "Rejection becomes impossible or the quorum is lost.",
            "The change request is finalized.",
        ]
    else:
        workflow_definition["completion_criteria"] = [
            "The source record reaches the expected end state.",
            "The workflow outcome is recorded.",
        ]

    return {
        "artifact_type": "workflow_plan",
        "name": workflow_name,
        "table": table,
        "description": f"Stepwise workflow plan for {workflow_name}.",
        "published": False,
        "workflow_definition": workflow_definition,
        "workflow_steps": steps,
        "script": None,
        "requested_artifact_type": normalize_artifact_hint(artifact_hint),
    }


def workflow_plan_looks_generic(plan):
    if not isinstance(plan, dict):
        return True

    workflow_steps = plan.get("workflow_steps") or []
    if not workflow_steps:
        return True

    generic_terms = (
        "helper",
        "gate",
        "trigger",
        "monitor",
        "finalize",
        "finaliser",
    )

    for step in workflow_steps:
        if not isinstance(step, dict):
            return True

        step_text = " ".join(
            [
                str(step.get("step_key") or ""),
                str(step.get("name") or ""),
                str(step.get("description") or ""),
                str(step.get("purpose") or ""),
            ]
        ).lower()

        if not any(term in step_text for term in generic_terms):
            return False

    return True


def normalize_workflow_plan_step(
    raw_step,
    requirement,
    context,
    provider,
    fallback_table=None,
    step_index=1,
    previous_step_key=None,
):
    if not isinstance(raw_step, dict):
        raw_step = {}

    artifact_type = normalize_artifact_type(raw_step.get("artifact_type"))
    name_blob = " ".join(
        [
            str(raw_step.get("name") or ""),
            str(raw_step.get("description") or ""),
            str(raw_step.get("purpose") or ""),
        ]
    ).lower()

    if artifact_type not in {"script_include", "business_rule", "client_script"}:
        if raw_step.get("type") or any(token in name_blob for token in ("change", "load", "submit", "client")):
            artifact_type = "client_script"
        elif raw_step.get("table") or raw_step.get("when") is not None:
            artifact_type = "business_rule"
        else:
            artifact_type = "script_include"

    step_key_source = raw_step.get("step_key") or raw_step.get("name") or f"step_{step_index}"
    step_key = sanitize_js_identifier(step_key_source, fallback=f"step_{step_index}").lower()
    name = raw_step.get("name") or step_key.replace("_", " ").title()
    description = raw_step.get("description") or raw_step.get("purpose") or f"Workflow step {step_index}"
    purpose = raw_step.get("purpose") or raw_step.get("description") or description

    depends_on = raw_step.get("depends_on")
    if not isinstance(depends_on, list):
        depends_on = [depends_on] if depends_on else []
    depends_on = [str(item) for item in depends_on if item]
    if not depends_on and previous_step_key:
        depends_on = [previous_step_key]

    when = raw_step.get("when")
    insert = raw_step.get("insert")
    update = raw_step.get("update")
    client_type = raw_step.get("type")

    if artifact_type == "script_include":
        when = None
        insert = False if insert is None else bool(insert)
        update = False if update is None else bool(update)
        client_type = None
    elif artifact_type == "business_rule":
        if when is None:
            if any(token in name_blob for token in ("monitor", "final", "complete", "evaluate", "close", "route", "notify")):
                when = "after"
            else:
                when = "before"

        if insert is None:
            insert = any(token in name_blob for token in ("gate", "intake", "trigger", "request", "create"))
        else:
            insert = bool(insert)

        if update is None:
            update = True
        else:
            update = bool(update)

        client_type = None
    else:
        if client_type is None:
            if any(token in name_blob for token in ("change", "update", "edit")):
                client_type = "onChange"
            elif any(token in name_blob for token in ("load", "initialize", "init")):
                client_type = "onLoad"
            else:
                client_type = "onSubmit"

        when = None
        insert = False if insert is None else bool(insert)
        update = False if update is None else bool(update)

    table = raw_step.get("table")
    if artifact_type in {"business_rule", "client_script"} and not table:
        inferred_table = fallback_table
        if not inferred_table:
            inferred_table = infer_missing_table(
                requirement=requirement,
                context=context,
                provider=provider,
                artifact_type=artifact_type,
                artifact_name=name,
                script="",
            )
        table = inferred_table
    elif artifact_type == "script_include":
        table = None

    return build_workflow_plan_step(
        step_key=step_key,
        name=name,
        artifact_type=artifact_type,
        table=table,
        when=when,
        insert=insert,
        update=update,
        type=client_type,
        description=description,
        purpose=purpose,
        depends_on=depends_on,
        order=raw_step.get("order") if raw_step.get("order") is not None else step_index,
    )


def generate_workflow_plan_artifact(requirement, context, provider, artifact_hint="workflow"):
    workflow_kind = infer_workflow_kind(requirement, context)
    prompt_text = build_workflow_plan_prompt(
        requirement=requirement,
        context=context,
        workflow_kind=workflow_kind,
        artifact_hint=artifact_hint,
    )

    write_debug_log(
        "workflow_plan_generation_start",
        {
            "requirement": requirement,
            "provider": provider,
            "artifact_hint": artifact_hint,
            "workflow_kind": workflow_kind,
            "context": context,
            "prompt": prompt_text,
            "request_timeout_seconds": settings.LLM_REQUEST_TIMEOUT_SECONDS,
        },
    )

    response = router.generate(
        [
            {
                "role": "system",
                "content": "You are a ServiceNow workflow planning assistant.",
            },
            {
                "role": "user",
                "content": prompt_text,
            },
        ],
        provider=provider,
    )

    cleaned = extract_json(response)

    write_debug_log(
        "workflow_plan_generation_raw_response",
        {
            "requirement": requirement,
            "provider": provider,
            "workflow_kind": workflow_kind,
            "response": response,
            "cleaned": cleaned,
        },
    )

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        repaired = repair_malformed_json_response(
            response=response,
            requirement=requirement,
            context=context,
            provider=provider,
            artifact_hint="workflow_plan",
            extra_instructions=(
                "Rebuild only the workflow plan as a workflow_plan object. "
                "Do not include scripts. "
                "The workflow_steps array must contain only step metadata."
            ),
        )

        if not repaired:
            return None

        cleaned = repaired
        try:
            data = json.loads(cleaned)
        except Exception:
            return None
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    normalized = dict(data)
    normalized["artifact_type"] = "workflow_plan"
    normalized["requested_artifact_type"] = "workflow"

    hinted_table = guess_table_from_text(
        requirement,
        context,
        normalized.get("name"),
        normalized.get("description"),
    )
    table = hinted_table or normalized.get("table")
    if not table:
        if workflow_kind == "approval":
            table = "change_request"
        elif workflow_kind in {"onboarding", "fulfillment"}:
            table = "sc_req_item"

    if not table:
        table = infer_missing_table(
            requirement=requirement,
            context=context,
            provider=provider,
            artifact_type="workflow_plan",
            artifact_name=normalized.get("name", ""),
            script="",
        )

    if table:
        normalized["table"] = table

    if not normalized.get("name"):
        normalized["name"] = derive_workflow_name(requirement, table, workflow_kind)
    else:
        current_name = str(normalized.get("name") or "").strip().lower()
        if workflow_kind == "approval" and "cab" in current_name and "cab" not in _normalize_workflow_text(f"{requirement} {context}").lower():
            normalized["name"] = derive_workflow_name(requirement, table, workflow_kind)

    if not normalized.get("description"):
        normalized["description"] = f"Stepwise workflow plan for {normalized['name']}."

    workflow_definition = normalized.get("workflow_definition")
    if not isinstance(workflow_definition, dict):
        workflow_definition = {}

    workflow_definition["goal"] = requirement
    workflow_definition["trigger"] = derive_workflow_trigger(
        requirement=requirement,
        context=context,
        workflow_kind=workflow_kind,
        table=table,
        subject=infer_workflow_subject(requirement, context, workflow_kind, table),
    )
    workflow_definition["deployment_mode"] = "stepwise"
    workflow_definition["workflow_kind"] = workflow_kind
    if workflow_kind == "approval":
        approval_threshold = infer_approval_threshold(requirement)
        approval_group = infer_approval_group_name(requirement, context)
        approval_subject = infer_workflow_subject(requirement, context, workflow_kind, table)
        if approval_threshold:
            workflow_definition["approval_threshold"] = approval_threshold
        if approval_group:
            workflow_definition["approval_group"] = approval_group
        if approval_subject:
            workflow_definition["approval_subject"] = approval_subject
    normalized["workflow_definition"] = workflow_definition

    if workflow_kind == "approval" and workflow_plan_has_suspicious_approval_structure(
        normalized,
        requirement=requirement,
        context=context,
        table=table,
    ):
        write_debug_log(
            "workflow_plan_generation_conflict_rejected",
            {
                "requirement": requirement,
                "provider": provider,
                "workflow_kind": workflow_kind,
                "plan": normalized,
            },
        )
        return None

    raw_steps = normalized.get("workflow_steps") or []
    plan_steps = []

    for index, raw_step in enumerate(raw_steps, start=1):
        previous_key = plan_steps[-1].get("step_key") if plan_steps else None
        normalized_step = normalize_workflow_plan_step(
            raw_step,
            requirement=requirement,
            context=context,
            provider=provider,
            fallback_table=table,
            step_index=index,
            previous_step_key=previous_key,
        )
        normalized_step = enforce_workflow_step_contract(
            normalized_step,
            workflow_kind=workflow_kind,
            target_table=table,
        )
        plan_steps.append(normalized_step)

    plan_steps = dedupe_workflow_steps(plan_steps)
    plan_steps.sort(key=workflow_step_order)

    for index, step in enumerate(plan_steps, start=1):
        step["order"] = index

    normalized["workflow_steps"] = plan_steps
    normalized["workflow_definition"]["step_count"] = len(plan_steps)
    normalized["script"] = None
    normalized["published"] = bool(normalized.get("published", False))
    normalized["requested_artifact_type"] = normalize_artifact_hint(artifact_hint)

    validation = validate_workflow_plan(normalized)
    write_debug_log(
        "workflow_plan_generation_normalized",
        {
            "requirement": requirement,
            "provider": provider,
            "workflow_kind": workflow_kind,
            "plan": normalized,
            "validation": validation,
        },
    )

    if not validation.get("valid"):
        return None

    if workflow_plan_looks_generic(normalized):
        write_debug_log(
            "workflow_plan_generation_generic_rejected",
            {
                "requirement": requirement,
                "provider": provider,
                "workflow_kind": workflow_kind,
                "plan": normalized,
            },
        )
        return None

    return normalized


def infer_missing_table(requirement, context, provider, artifact_type, artifact_name="", script=""):
    # Prefer deterministic hints first, then let the LLM repair the omission.
    write_debug_log(
        "table_inference_start",
        {
            "artifact_type": artifact_type,
            "artifact_name": artifact_name,
            "requirement": requirement,
            "context": context,
        },
    )

    hinted_table = guess_table_from_text(requirement, context, artifact_name, script)
    if hinted_table:
        write_debug_log(
            "table_inference_deterministic",
            {
                "artifact_type": artifact_type,
                "artifact_name": artifact_name,
                "table": hinted_table,
            },
        )
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

    write_debug_log(
        "table_inference_llm_response",
        {
            "artifact_type": artifact_type,
            "artifact_name": artifact_name,
            "response": repair_response,
            "cleaned": cleaned,
        },
    )

    try:
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            repaired = repair_malformed_json_response(
                response=repair_response,
                requirement=requirement,
                context=context,
                provider=provider,
                artifact_hint=artifact_type,
            )

            if repaired:
                cleaned = repaired
                data = json.loads(cleaned)
            else:
                raise
        table = data.get("table")
        if table and str(table).strip().lower() != "null":
            write_debug_log(
                "table_inference_result",
                {
                    "artifact_type": artifact_type,
                    "artifact_name": artifact_name,
                    "table": table,
                },
            )
            return str(table).strip()
    except Exception:
        write_debug_log(
            "table_inference_error",
            {
                "artifact_type": artifact_type,
                "artifact_name": artifact_name,
                "response": repair_response,
                "cleaned": cleaned,
            },
        )

    return None


# ---------------- MAIN FUNCTION ----------------
def generate_script(
    requirement,
    provider="gemini",
    context="",
    artifact_hint="auto",
):
    requested_artifact_type = normalize_artifact_hint(artifact_hint)

    if is_cab_quorum_workflow_requirement(requirement, context, requested_artifact_type):
        workflow_artifact = build_cab_approval_workflow_artifact(requirement, context)

        write_debug_log(
            "workflow_generation_template_used",
            {
                "requirement": requirement,
                "provider": provider,
                "artifact_hint": requested_artifact_type,
                "workflow_name": workflow_artifact.get("name"),
                "reason": "CAB 5-of-8 workflow recognized locally",
            },
        )

        return build_workflow_artifact(
            data=workflow_artifact,
            requirement=requirement,
            context=context,
            provider=provider,
        )

    prompt_text = build_prompt(
        requirement=requirement,
        context=context,
        artifact_hint=requested_artifact_type,
    )

    write_debug_log(
        "workflow_generation_start",
        {
            "requirement": requirement,
            "provider": provider,
            "artifact_hint": requested_artifact_type,
            "context": context,
            "prompt": prompt_text,
            "request_timeout_seconds": settings.LLM_REQUEST_TIMEOUT_SECONDS,
        },
    )

    messages = [
        {
            "role": "system",
            "content": "You are a ServiceNow expert developer."
        },
        {
            "role": "user",
            "content": prompt_text,
        }
    ]

    response = router.generate(messages, provider=provider)

    print("\n[RAW LLM RESPONSE]\n", response)

    write_debug_log(
        "workflow_generation_raw_response",
        {
            "requirement": requirement,
            "provider": provider,
            "artifact_hint": requested_artifact_type,
            "response": response,
        },
    )

    try:
        # 🔥 CLEAN RESPONSE FIRST
        cleaned = extract_json(response)

        print("\n[CLEANED JSON]\n", cleaned)

        write_debug_log(
            "workflow_generation_cleaned_response",
            {
                "requirement": requirement,
                "provider": provider,
                "artifact_hint": requested_artifact_type,
                "cleaned": cleaned,
            },
        )

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

            write_debug_log(
                "workflow_generation_wrapped_step",
                {
                    "requirement": requirement,
                    "provider": provider,
                    "artifact_hint": requested_artifact_type,
                    "wrapped_step": source_step,
                },
            )

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

        write_debug_log(
            "workflow_generation_post_process",
            {
                "requirement": requirement,
                "provider": provider,
                "artifact_hint": requested_artifact_type,
                "artifact": data,
            },
        )

        artifact = Artifact.model_validate(data)

        if artifact.artifact_type == "workflow":
            if not artifact.workflow_steps or len(artifact.workflow_steps) < MIN_WORKFLOW_STEPS:
                raise ValueError(
                    f"Workflow artifacts require at least {MIN_WORKFLOW_STEPS} deployable workflow steps."
                )
        elif artifact_requires_table(artifact.artifact_type) and not artifact.table:
            raise ValueError(
                "Could not infer a target table from the requirement. "
                "Please restate the requirement with the record type you want to target."
            )

        if artifact.artifact_type in {"business_rule", "script_include", "client_script"} and not artifact.script:
            raise ValueError("Generated artifact is missing script text")

        write_debug_log(
            "workflow_generation_final_artifact",
            {
                "requirement": requirement,
                "provider": provider,
                "artifact_hint": requested_artifact_type,
                "artifact": artifact.model_dump(),
            },
        )

        return artifact.model_dump()

    except ValidationError as e:
        print("\n[SCHEMA ERROR]\n", str(e))

        write_debug_log(
            "workflow_generation_schema_error",
            {
                "requirement": requirement,
                "provider": provider,
                "artifact_hint": requested_artifact_type,
                "error": str(e),
            },
        )

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

        write_debug_log(
            "workflow_generation_error",
            {
                "requirement": requirement,
                "provider": provider,
                "artifact_hint": requested_artifact_type,
                "error": str(e),
                "raw_response": response if "response" in locals() else None,
            },
        )

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
