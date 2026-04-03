import requests
from config.settings import settings
from datetime import datetime, timezone
from pathlib import Path
import json
import re

ARTIFACT_TABLES = {
    "business_rule": "sys_script",
    "script_include": "sys_script_include",
    "client_script": "sys_script_client",
    "workflow": "wf_workflow",
}

DEBUG_LOG_PATH = Path(__file__).resolve().parents[1] / "logs" / "workflow_debug.txt"
REQUEST_TIMEOUT = 60


def build_http_session():
    session = requests.Session()
    session.trust_env = False
    return session


def redact_sensitive_text(text):
    if not text:
        return ""

    redacted = str(text)
    redacted = re.sub(r'("access_token"\s*:\s*")[^"]+(")', r'\1[redacted]\2', redacted)
    redacted = re.sub(r'("client_secret"\s*:\s*")[^"]+(")', r'\1[redacted]\2', redacted)

    return redacted


def write_debug_log(event, details):
    try:
        DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(details, ensure_ascii=True, default=str)

        with DEBUG_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] {event} {payload}\n")
    except Exception:
        pass


def truncate(text, limit=1000):
    if text is None:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def summarize_artifact_for_log(artifact):
    if not isinstance(artifact, dict):
        return {"artifact_repr": truncate(artifact, 500)}

    script = artifact.get("script", "")
    workflow_steps = artifact.get("workflow_steps") or []

    return {
        "artifact_type": artifact.get("artifact_type"),
        "requested_artifact_type": artifact.get("requested_artifact_type"),
        "name": artifact.get("name"),
        "table": artifact.get("table"),
        "requested_table": artifact.get("requested_table"),
        "when": artifact.get("when"),
        "insert": artifact.get("insert"),
        "update": artifact.get("update"),
        "type": artifact.get("type"),
        "script_length": len(script) if isinstance(script, str) else None,
        "workflow_step_count": len(workflow_steps) if isinstance(workflow_steps, list) else None,
    }


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


def coerce_bool(value, default=False):
    if value is None:
        return default

    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}

    return bool(value)


def resolve_target_table(artifact_type):
    target_table = ARTIFACT_TABLES.get(normalize_artifact_type(artifact_type))

    if not target_table:
        raise ValueError(f"Unsupported artifact type: {artifact_type}")

    return target_table


def build_workflow_description(artifact):
    description = artifact.get("description")

    if not description:
        workflow_definition = artifact.get("workflow_definition")

        if isinstance(workflow_definition, (dict, list)):
            try:
                description = json.dumps(workflow_definition, ensure_ascii=True, indent=2, default=str)
            except Exception:
                description = str(workflow_definition)
        elif workflow_definition:
            description = str(workflow_definition)
        else:
            description = "Generated workflow"

    step_names = []
    for step in artifact.get("workflow_steps") or []:
        if isinstance(step, dict) and step.get("name"):
            step_names.append(step["name"])

    if step_names:
        description = f"{description}\nSteps: {', '.join(step_names)}"

    return truncate(description, 4000)


def build_payload(artifact):
    artifact_type = normalize_artifact_type(
        artifact.get("artifact_type") or artifact.get("requested_artifact_type")
    )

    table = resolve_target_table(artifact_type)

    if artifact_type == "workflow":
        target_table = artifact.get("table") or artifact.get("requested_table")

        if not target_table:
            raise ValueError("Workflows require a target table")

        body = {
            "name": artifact.get("name") or "generated_workflow",
            "table": target_table,
            "description": build_workflow_description(artifact),
        }

        if artifact.get("published") is not None:
            body["published"] = coerce_bool(artifact.get("published"), False)

        return table, body

    body = {
        "name": artifact.get("name") or "generated_script",
        "script": artifact.get("script") or "",
        "active": True,
    }

    if artifact_type == "business_rule":
        target_table = artifact.get("table") or artifact.get("requested_table")

        if not target_table:
            raise ValueError("Business rules require a target table")

        body.update({
            "collection": target_table,
            "when": (artifact.get("when") or "after").strip().lower(),
            "insert": coerce_bool(artifact.get("insert"), True),
            "update": coerce_bool(artifact.get("update"), True),
            "advanced": True,
        })

        if artifact.get("order") is not None:
            body["order"] = artifact.get("order")

    elif artifact_type == "client_script":
        target_table = artifact.get("table") or artifact.get("requested_table")

        if not target_table:
            raise ValueError("Client scripts require a target table")

        body["table"] = target_table

        if artifact.get("type"):
            body["type"] = artifact.get("type")

    elif artifact_type == "script_include":
        pass

    return table, body


def send_with_fallback(url, headers, payload_candidates):
    errors = []
    session = build_http_session()

    for index, payload in enumerate(payload_candidates, start=1):
        write_debug_log(
            "deploy_attempt",
            {
                "attempt": index,
                "url": url,
                "target_table": payload.get("collection") or payload.get("table"),
                "payload_keys": sorted(payload.keys()),
            },
        )

        try:
            response = session.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()

            write_debug_log(
                "deploy_success",
                {
                    "attempt": index,
                    "status_code": response.status_code,
                    "response": truncate(response.text, 2000),
                },
            )

            return response
        except requests.RequestException as exc:
            response = getattr(exc, "response", None)
            error_text = truncate(response.text if response is not None else str(exc), 2000)

            write_debug_log(
                "deploy_failure",
                {
                    "attempt": index,
                    "status_code": getattr(response, "status_code", None),
                    "response": error_text,
                },
            )

            errors.append(error_text)

    raise RuntimeError("ServiceNow deploy failed: " + " | ".join(errors))


# ---------------- TOKEN ----------------
def get_oauth_token():

    instance = (settings.SN_INSTANCE or "").rstrip("/")

    if not instance:
        raise ValueError("SN_INSTANCE is not configured")

    url = f"{instance}/oauth_token.do"
    session = build_http_session()

    data = {
        "grant_type": "client_credentials",
        "client_id": settings.SN_CLIENT_ID,
        "client_secret": settings.SN_CLIENT_SECRET
    }

    r = session.post(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=REQUEST_TIMEOUT
    )

    print("\n[OAUTH RESPONSE]", redact_sensitive_text(r.text))

    try:
        r.raise_for_status()
    except requests.RequestException as exc:
        write_debug_log(
            "oauth_failure",
            {
                "status_code": r.status_code,
                "response": truncate(r.text, 2000),
            },
        )
        raise RuntimeError(f"ServiceNow OAuth failed: {r.status_code} {truncate(r.text, 500)}") from exc

        write_debug_log(
            "oauth_success",
            {
                "status_code": r.status_code,
                "content_type": r.headers.get("Content-Type"),
                "response_preview": redact_sensitive_text(truncate(r.text, 500)),
            },
        )

    if "Instance Hibernating page" in r.text:
        write_debug_log(
            "oauth_hibernating",
            {
                "status_code": r.status_code,
                "content_type": r.headers.get("Content-Type"),
                "response": truncate(r.text, 2000),
            },
        )
        raise RuntimeError(
            "ServiceNow instance is hibernating. Wake the instance and retry the deployment."
        )

    try:
        return r.json().get("access_token")
    except ValueError as exc:
        write_debug_log(
            "oauth_invalid_json",
            {
                "status_code": r.status_code,
                "content_type": r.headers.get("Content-Type"),
                "response": truncate(r.text, 2000),
            },
        )
        raise RuntimeError("ServiceNow OAuth returned a non-JSON response") from exc


# ---------------- HEADERS ----------------
def get_headers():

    token = get_oauth_token()

    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }


def lookup_group_sys_id(*name_hints, instance=None, headers=None):
    instance = (instance or settings.SN_INSTANCE or "").rstrip("/")

    if not instance:
        return None

    session = build_http_session()
    headers = headers or get_headers()

    cleaned_hints = [hint.strip() for hint in name_hints if isinstance(hint, str) and hint.strip()]
    if not cleaned_hints:
        return None

    queries = []
    for hint in cleaned_hints:
        queries.extend([
            f"name={hint}",
            f"nameLIKE{hint}",
            f"descriptionLIKE{hint}",
        ])

    for query in queries:
        try:
            write_debug_log(
                "group_lookup_attempt",
                {
                    "query": query,
                },
            )

            response = session.get(
                f"{instance}/api/now/table/sys_user_group",
                headers=headers,
                params={
                    "sysparm_query": query,
                    "sysparm_fields": "sys_id,name,description",
                    "sysparm_limit": "10",
                },
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()

            result = response.json().get("result") or []
            if not result:
                continue

            lower_hints = {hint.lower() for hint in cleaned_hints}
            exact_match = None
            for row in result:
                name = str(row.get("name") or "").strip().lower()
                if name in lower_hints:
                    exact_match = row
                    break

            chosen = exact_match or result[0]

            write_debug_log(
                "group_lookup_success",
                {
                    "query": query,
                    "group": chosen,
                },
            )

            return chosen.get("sys_id")
        except Exception as exc:
            write_debug_log(
                "group_lookup_failure",
                {
                    "query": query,
                    "error": truncate(str(exc), 1000),
                },
            )

    return None


def deploy_single_artifact(artifact, headers=None, instance=None):
    instance = (instance or settings.SN_INSTANCE or "").rstrip("/")

    if not instance:
        raise ValueError("SN_INSTANCE is not configured")

    table, body = build_payload(artifact)

    headers = headers or get_headers()

    url = f"{instance}/api/now/table/{table}"

    payload_candidates = [body]

    if normalize_artifact_type(artifact.get("artifact_type")) == "business_rule":
        fallback = dict(body)
        fallback["table"] = fallback.pop("collection")
        payload_candidates.append(fallback)

    response = send_with_fallback(url, headers, payload_candidates)

    print("\n[SN RESPONSE]", response.text)

    try:
        return response.json()
    except ValueError:
        write_debug_log(
            "deploy_non_json_response",
            {
                "status_code": response.status_code,
                "content_type": response.headers.get("Content-Type"),
                "response": truncate(response.text, 2000),
            },
        )
        return {
            "status_code": response.status_code,
            "content_type": response.headers.get("Content-Type"),
            "response_text": truncate(response.text, 2000),
        }


def workflow_step_order(step):
    if not isinstance(step, dict):
        return 10**9

    try:
        return int(step.get("order"))
    except Exception:
        return 10**9


# ---------------- DEPLOY ----------------
def deploy_artifact(artifact, headers=None, instance=None):

    try:
        instance = (instance or settings.SN_INSTANCE or "").rstrip("/")

        if not instance:
            raise ValueError("SN_INSTANCE is not configured")

        artifact_type = normalize_artifact_type(artifact.get("artifact_type"))

        if artifact_type == "workflow":
            headers = headers or get_headers()

            workflow_record = deploy_single_artifact(artifact, headers=headers, instance=instance)

            workflow_steps = artifact.get("workflow_steps") or []
            ordered_steps = sorted(
                workflow_steps,
                key=workflow_step_order,
            )

            step_results = []
            for index, step in enumerate(ordered_steps, start=1):
                if not isinstance(step, dict):
                    continue

                write_debug_log(
                    "workflow_step_start",
                    {
                        "workflow_name": artifact.get("name"),
                        "step_index": index,
                        "step": summarize_artifact_for_log(step),
                    },
                )

                step_result = deploy_artifact(step, headers=headers, instance=instance)
                step_results.append(
                    {
                        "step_index": index,
                        "name": step.get("name"),
                        "artifact_type": step.get("artifact_type"),
                        "result": step_result,
                    }
                )

                write_debug_log(
                    "workflow_step_success",
                    {
                        "workflow_name": artifact.get("name"),
                        "step_index": index,
                        "step_name": step.get("name"),
                    },
                )

            return {
                "workflow_record": workflow_record,
                "workflow_steps": step_results,
            }

        return deploy_single_artifact(artifact, headers=headers, instance=instance)
    except Exception as exc:
        write_debug_log(
            "deploy_artifact_exception",
            {
                "artifact": summarize_artifact_for_log(artifact),
                "error": truncate(str(exc), 2000),
            },
        )
        raise
