import sys
import os
import json
import re

# 🔥 Ensure project root is in PYTHONPATH
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import streamlit as st

from agent.orchestrator import generate_script
from agent.orchestrator import build_workflow_plan, generate_workflow_step_artifact, infer_workflow_kind
from integration.servicenow_client import deploy_artifact, write_debug_log
from config.settings import settings
from llm.errors import LLMProviderError, format_generation_error


# ---------------- SAFE IMPORTS ----------------
try:
    from validation.script_validator import validate_script, validate_workflow_plan
except:
    def validate_script(x):
        return {"valid": True, "issues": []}

    def validate_workflow_plan(x):
        return {"valid": True, "issues": []}


try:
    from rag.retriever import retrieve_context
except:
    def retrieve_context(x):
        return ""


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


def normalize_generated_artifact(artifact):
    if not isinstance(artifact, dict):
        return artifact

    artifact_type = str(artifact.get("artifact_type") or "").strip().lower()
    script = artifact.get("script")

    if artifact_type in {"unknown", ""} and isinstance(script, str):
        parsed = extract_json_blob(script)
        if isinstance(parsed, dict) and parsed.get("artifact_type"):
            parsed["requested_artifact_type"] = artifact.get(
                "requested_artifact_type",
                parsed.get("requested_artifact_type"),
            )
            write_debug_log(
                "ui_artifact_unwrapped",
                {
                    "original_artifact_type": artifact.get("artifact_type"),
                    "original_name": artifact.get("name"),
                    "unwrapped_artifact_type": parsed.get("artifact_type"),
                    "unwrapped_name": parsed.get("name"),
                },
            )
            return parsed

    return artifact


def get_workflow_state():
    if "workflow_state" not in st.session_state:
        st.session_state.workflow_state = {
            "requirement": None,
            "provider": None,
            "context": None,
            "plan": None,
            "plan_validation": None,
            "step_index": 0,
            "history": [],
            "status": "idle",
            "last_error": None,
            "last_result": None,
        }

    return st.session_state.workflow_state


def reset_workflow_state():
    st.session_state.workflow_state = {
        "requirement": None,
        "provider": None,
        "context": None,
        "plan": None,
        "plan_validation": None,
        "step_index": 0,
        "history": [],
        "status": "idle",
        "last_error": None,
        "last_result": None,
    }


def render_workflow_wizard(requirement, provider, context=None):
    state = get_workflow_state()

    st.subheader("Workflow Wizard")
    st.caption("Generate one step, deploy it, then unlock the next step.")

    if state.get("plan") and state.get("requirement") and state.get("requirement") != requirement:
        reset_workflow_state()
        state = get_workflow_state()

    if state.get("plan") and state.get("provider") and state.get("provider") != provider:
        reset_workflow_state()
        state = get_workflow_state()

    if st.button("Rebuild Workflow Plan", key="workflow_rebuild_plan"):
        reset_workflow_state()
        st.rerun()

    if not state.get("plan"):
        workflow_context = context or ""
        plan = build_workflow_plan(
            requirement=requirement,
            context=workflow_context,
            artifact_hint="workflow",
            provider=provider,
        )
        plan_validation = validate_workflow_plan(plan)

        write_debug_log(
            "ui_workflow_plan_result",
            {
                "requirement": requirement,
                "provider": provider,
                "plan": plan,
                "validation": plan_validation,
            },
        )

        if not plan_validation.get("valid"):
            state["plan"] = None
            state["plan_validation"] = plan_validation
            state["status"] = "failed"
            state["last_error"] = "Workflow plan failed validation"
            st.error("Workflow plan failed validation: " + "; ".join(plan_validation.get("issues", [])))
            return

        state.update(
            {
                "requirement": requirement,
                "provider": provider,
                "context": workflow_context,
                "plan": plan,
                "plan_validation": plan_validation,
                "step_index": 0,
                "history": [],
                "status": "planned",
                "last_error": None,
                "last_result": None,
            }
        )

        st.success("Workflow plan ready. Use the next button to generate and deploy each step.")

    plan = state.get("plan")
    if not plan:
        st.info("Build a workflow plan to start the step-by-step deployment flow.")
        return

    st.success("Workflow Plan Ready")
    st.write(f"Workflow: {plan.get('name', '')}")
    if plan.get("description"):
        st.write(plan.get("description"))
    if plan.get("table"):
        st.code(plan.get("table"))

    if plan.get("workflow_definition"):
        st.subheader("Workflow Definition")
        st.json(plan.get("workflow_definition"))

    workflow_steps = plan.get("workflow_steps") or []
    total_steps = len(workflow_steps)
    completed_steps = len([entry for entry in state.get("history", []) if entry.get("status") == "deployed"])

    if total_steps:
        st.progress(min(completed_steps / total_steps, 1.0))
        st.caption(f"Step {min(state.get('step_index', 0) + 1, total_steps)} of {total_steps}")

    if state.get("last_error"):
        st.error(f"Last step failed: {state.get('last_error')}")

    if state.get("history"):
        st.subheader("Deployment History")
        for entry in state.get("history", []):
            with st.expander(f"Step {entry.get('step_index')}: {entry.get('step_name')} - {entry.get('status')}"):
                st.json(entry)

    st.subheader("Workflow Steps")
    for index, step in enumerate(workflow_steps, start=1):
        with st.expander(f"Step {index}: {step.get('name', 'Unnamed Step')}"):
            st.write(f"Artifact Type: {step.get('artifact_type', '')}")
            if step.get("table"):
                st.write(f"Table: {step.get('table')}")
            if step.get("purpose"):
                st.write(f"Purpose: {step.get('purpose')}")
            if step.get("description"):
                st.write(step.get("description"))
            if step.get("depends_on"):
                st.write(f"Depends on: {', '.join(step.get('depends_on', []))}")

    if state.get("step_index", 0) >= total_steps:
        st.success("All workflow steps have been deployed.")
    else:
        current_index = state.get("step_index", 0)
        current_step = workflow_steps[current_index]

        st.subheader(f"Current Step: {current_step.get('name', f'Step {current_index + 1}')}")
        st.write(current_step.get("purpose") or current_step.get("description") or "Deploy this step next.")

        if state.get("status") == "failed":
            action_label = f"Retry Step {current_index + 1}"
        elif current_index == 0 and not state.get("history"):
            action_label = "Start Step 1"
        else:
            action_label = "Next Step"

        if st.button(action_label, key=f"workflow_step_action_{current_index}"):
            context = state.get("context") or ""
            prior_steps = [
                entry.get("artifact")
                for entry in state.get("history", [])
                if entry.get("status") == "deployed" and isinstance(entry.get("artifact"), dict)
            ]

            write_debug_log(
                "ui_workflow_step_request",
                {
                    "requirement": requirement,
                    "provider": provider,
                    "workflow_name": plan.get("name"),
                    "step_index": current_index + 1,
                    "current_step": current_step,
                    "prior_step_count": len(prior_steps),
                },
            )

            try:
                artifact = generate_workflow_step_artifact(
                    requirement=requirement,
                    context=context,
                    provider=provider,
                    workflow_plan=plan,
                    step_index=current_index,
                    prior_steps=prior_steps,
                )
            except Exception as exc:
                friendly_error = format_generation_error(exc)
                state["status"] = "failed"
                state["last_error"] = f"Generation failed for step {current_index + 1}: {friendly_error}"
                state["history"].append(
                    {
                        "step_index": current_index + 1,
                        "step_name": current_step.get("name"),
                        "status": "generation_failed",
                        "error": friendly_error,
                        "step": current_step,
                    }
                )
                write_debug_log(
                    "ui_workflow_step_error",
                    {
                        "requirement": requirement,
                        "provider": provider,
                        "workflow_name": plan.get("name"),
                        "step_index": current_index + 1,
                        "stage": "generation",
                        "error": str(exc),
                        "friendly_error": friendly_error,
                    },
                )
                st.error(f"Step {current_index + 1} generation failed: {friendly_error}")
                st.caption("Fix the issue or click Retry Step after updating the prompt/inputs.")
                return

            validation = validate_script(artifact)
            if isinstance(validation, list):
                validation = {
                    "valid": len(validation) == 0,
                    "issues": validation,
                }
            elif not isinstance(validation, dict):
                validation = {
                    "valid": True,
                    "issues": [],
                }

            if not validation.get("valid"):
                validation_error = "; ".join(validation.get("issues", ["workflow step failed validation"]))
                state["status"] = "failed"
                state["last_error"] = f"Validation failed for step {current_index + 1}: {validation_error}"
                state["history"].append(
                    {
                        "step_index": current_index + 1,
                        "step_name": current_step.get("name"),
                        "status": "validation_failed",
                        "error": validation_error,
                        "step": current_step,
                        "artifact": artifact,
                        "validation": validation,
                    }
                )
                write_debug_log(
                    "ui_workflow_step_error",
                    {
                        "requirement": requirement,
                        "provider": provider,
                        "workflow_name": plan.get("name"),
                        "step_index": current_index + 1,
                        "stage": "validation",
                        "validation": validation,
                        "artifact": artifact,
                    },
                )
                st.error(f"Step {current_index + 1} validation failed: {validation_error}")
                return

            try:
                result = deploy_artifact(artifact)
            except Exception as exc:
                state["status"] = "failed"
                state["last_error"] = f"Deployment failed for step {current_index + 1}: {str(exc)}"
                state["history"].append(
                    {
                        "step_index": current_index + 1,
                        "step_name": current_step.get("name"),
                        "status": "deployment_failed",
                        "error": str(exc),
                        "step": current_step,
                        "artifact": artifact,
                        "validation": validation,
                    }
                )
                write_debug_log(
                    "ui_workflow_step_error",
                    {
                        "requirement": requirement,
                        "provider": provider,
                        "workflow_name": plan.get("name"),
                        "step_index": current_index + 1,
                        "stage": "deployment",
                        "error": str(exc),
                        "artifact": artifact,
                    },
                )
                st.error(f"Step {current_index + 1} deployment failed: {str(exc)}")
                st.caption("The workflow stopped at the failing step. Fix the issue before continuing.")
                return

            state["history"].append(
                {
                    "step_index": current_index + 1,
                    "step_name": current_step.get("name"),
                    "status": "deployed",
                    "step": current_step,
                    "artifact": artifact,
                    "validation": validation,
                    "result": result,
                }
            )
            state["last_error"] = None
            state["last_result"] = result
            state["step_index"] = current_index + 1
            state["status"] = "completed" if state["step_index"] >= total_steps else "planned"

            write_debug_log(
                "ui_workflow_step_result",
                {
                    "requirement": requirement,
                    "provider": provider,
                    "workflow_name": plan.get("name"),
                    "step_index": current_index + 1,
                    "artifact": artifact,
                    "result": result,
                },
            )

            st.success(f"Step {current_index + 1} deployed successfully.")
            st.json(result)
            st.rerun()

    if st.button("Reset Workflow Wizard", key="workflow_reset"):
        reset_workflow_state()
        st.rerun()


# ---------------- SESSION STATE ----------------
if "artifact" not in st.session_state:
    st.session_state.artifact = None


# ---------------- UI ----------------
st.set_page_config(page_title="AI ServiceNow Developer Agent", layout="wide")
st.title("🚀 AI ServiceNow Developer Agent")


# ---------------- INPUT ----------------
requirement = st.text_area("Describe your ServiceNow requirement", height=150)

col1, col2 = st.columns(2)

with col1:
    selected_artifact_type = st.selectbox(
        "Artifact Type",
        ["auto", "business_rule", "script_include", "client_script", "workflow"]
    )

with col2:
    providers = ["openai", "gemini", "claude"]

    default_index = 0
    if settings.DEFAULT_PROVIDER in providers:
        default_index = providers.index(settings.DEFAULT_PROVIDER)

    provider = st.selectbox("LLM Provider", providers, index=default_index)


# ---------------- WORKFLOW ROUTING ----------------
workflow_context = None
workflow_mode = selected_artifact_type == "workflow"

if not workflow_mode and selected_artifact_type == "auto" and requirement.strip():
    workflow_mode = infer_workflow_kind(requirement, "") != "generic"

if workflow_mode:
    if not requirement.strip():
        st.warning("Please enter a requirement")
    else:
        st.session_state.artifact = None
        render_workflow_wizard(requirement, provider, context=workflow_context)
    st.stop()


# ---------------- GENERATE ----------------
if st.button("Generate Script"):

    if not requirement.strip():
        st.warning("Please enter a requirement")
        st.stop()

    write_debug_log(
        "ui_generate_request",
        {
            "requirement": requirement,
            "artifact_hint": selected_artifact_type,
            "provider": provider,
            "request_timeout_seconds": settings.LLM_REQUEST_TIMEOUT_SECONDS,
        },
    )

    with st.spinner("Generating script..."):
        try:
            context = retrieve_context(requirement)

            artifact = generate_script(
                requirement=requirement,
                provider=provider,
                context=context,
                artifact_hint=selected_artifact_type,
            )

            artifact = normalize_generated_artifact(artifact)
            artifact["requested_artifact_type"] = selected_artifact_type

            if selected_artifact_type != "auto":
                artifact["artifact_type"] = selected_artifact_type

            validation = validate_script(artifact)

            if isinstance(validation, list):
                validation = {
                    "valid": len(validation) == 0,
                    "issues": validation
                }
            elif not isinstance(validation, dict):
                validation = {
                    "valid": True,
                    "issues": []
                }

            if not validation.get("valid"):
                st.session_state.artifact = None
                write_debug_log(
                    "ui_generate_error",
                    {
                        "requirement": requirement,
                        "artifact_hint": selected_artifact_type,
                        "provider": provider,
                        "error": "Generated artifact failed validation",
                        "validation": validation,
                        "artifact": artifact,
                        "request_timeout_seconds": settings.LLM_REQUEST_TIMEOUT_SECONDS,
                    },
                )
                st.error(
                    "Script generation failed: " + "; ".join(validation.get("issues", ["generated artifact failed validation"]))
                )
            else:
                st.session_state.artifact = artifact

                write_debug_log(
                    "ui_generate_result",
                    {
                        "requirement": requirement,
                        "artifact_hint": selected_artifact_type,
                        "provider": provider,
                        "artifact": artifact,
                    },
                )

        except Exception as e:
            friendly_error = format_generation_error(e)
            write_debug_log(
                "ui_generate_error",
                {
                    "requirement": requirement,
                    "artifact_hint": selected_artifact_type,
                    "provider": provider,
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "friendly_error": friendly_error,
                    "request_timeout_seconds": settings.LLM_REQUEST_TIMEOUT_SECONDS,
                    "provider_error_kind": e.error_kind if isinstance(e, LLMProviderError) else None,
                    "provider_status_code": e.status_code if isinstance(e, LLMProviderError) else None,
                    "provider_raw_error": e.raw_error if isinstance(e, LLMProviderError) else None,
                },
            )
            st.error(f"Script generation failed: {friendly_error}")


# ---------------- DISPLAY ----------------
artifact = normalize_generated_artifact(st.session_state.artifact)

if artifact is not st.session_state.artifact:
    st.session_state.artifact = artifact

if artifact:

    artifact_type_value = str(artifact.get("artifact_type", "")).lower()

    if artifact_type_value == "workflow":
        st.success("Workflow Plan Generated")
    else:
        st.success("Script Generated")

    # Normalize display
    artifact_type_display = artifact_type_value.replace("_", " ")

    st.subheader("Artifact Type")
    st.code(artifact_type_display)

    st.subheader("Name")
    st.code(artifact.get("name", ""))

    if artifact.get("description"):
        st.subheader("Description")
        st.write(artifact.get("description"))

    if artifact.get("table"):
        st.subheader("Table")
        st.code(artifact.get("table", ""))

    script = artifact.get("script", "")

    if artifact.get("workflow_definition"):
        st.subheader("Workflow Definition")
        st.json(artifact.get("workflow_definition"))

    if artifact.get("workflow_steps"):
        st.subheader("Workflow Steps")
        for index, step in enumerate(artifact.get("workflow_steps", []), start=1):
            with st.expander(f"Step {index}: {step.get('name', 'Unnamed Step')}"):
                st.write(f"Artifact Type: {step.get('artifact_type', '')}")
                if step.get("table"):
                    st.write(f"Table: {step.get('table')}")
                if step.get("description"):
                    st.write(step.get("description"))
                if step.get("script"):
                    step_script = step.get("script", "")
                    if isinstance(step_script, str):
                        step_script = step_script.replace("\\n", "\n").replace("\\t", "\t")
                    st.code(step_script, language="javascript")
                else:
                    st.write("No script body for this step.")

        st.caption(f"Sequential workflow steps: {len(artifact.get('workflow_steps', []))}")

    if script:
        # ---------------- SCRIPT FORMAT FIX ----------------
        st.subheader("Generated Script")

        if isinstance(script, str):
            script = script.replace("\\n", "\n").replace("\\t", "\t")

        st.code(script, language="javascript")

    # ---------------- VALIDATION FIX ----------------
    validation = validate_script(artifact)

    if isinstance(validation, list):
        validation = {
            "valid": len(validation) == 0,
            "issues": validation
        }
    elif not isinstance(validation, dict):
        validation = {
            "valid": True,
            "issues": []
        }

    write_debug_log(
        "ui_validation_result",
        {
            "artifact": artifact,
            "validation": validation,
        },
    )

    if validation.get("valid"):
        st.success("Validation Passed")
    else:
        st.warning("Validation Issues Found")
        for issue in validation.get("issues", []):
            st.write(f"- {issue}")

    # ---------------- DEPLOY ----------------
    st.markdown("### 🚀 Deploy")

    deploy_ready = validation.get("valid", True)

    if not deploy_ready:
        st.warning("Resolve the validation issues before deploying this workflow.")

    if st.button("Deploy to ServiceNow", disabled=not deploy_ready):

        write_debug_log(
            "ui_deploy_request",
            {
                "artifact": artifact,
            },
        )

        with st.spinner("Deploying..."):
            try:
                result = deploy_artifact(artifact)

                write_debug_log(
                    "ui_deploy_result",
                    {
                        "artifact": artifact,
                        "result": result,
                    },
                )

                st.success("Deployment Successful")
                st.json(result)

            except Exception as e:
                write_debug_log(
                    "ui_deploy_error",
                    {
                        "artifact": artifact,
                        "error": str(e),
                    },
                )
                st.error(f"Deployment failed: {str(e)}")
                st.caption("Sanitized details were written to logs/workflow_debug.txt")


# ---------------- FOOTER ----------------
st.markdown("---")
st.caption("Built for ServiceNow AI Automation")
