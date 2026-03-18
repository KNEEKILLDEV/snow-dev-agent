import sys
import os

# Ensure project root is in path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import streamlit as st

from agent.orchestrator import generate_script
from integration.servicenow_client import deploy_artifact
from validation.script_validator import validate_script
from rag.retriever import retrieve_context

# ✅ RAG auto-init
try:
    from rag.ingest_instance import ingest_sample
    ingest_sample()
except Exception as e:
    print("RAG init skipped:", e)


# ---------------- UI ---------------- #

st.set_page_config(page_title="AI ServiceNow Developer Agent")

st.title("🚀 AI ServiceNow Developer Agent")


# ✅ Default = OpenAI
provider = st.selectbox(
    "Select AI Provider",
    ["openai", "gemini", "claude"],
    index=0
)


requirement = st.text_area(
    "Describe your ServiceNow requirement",
    height=150
)


# ---------------- RAG ---------------- #

if st.button("Show RAG Context"):

    if not requirement.strip():
        st.warning("Please enter a requirement")
    else:
        try:
            context = retrieve_context(requirement)
            st.subheader("Retrieved Context")
            st.code(context)

        except Exception as e:
            st.error(f"RAG retrieval failed: {e}")


# ---------------- Generation ---------------- #

if st.button("Generate Script"):

    if not requirement.strip():
        st.warning("Please enter a requirement")
    else:
        try:
            artifact = generate_script(requirement, provider)
            st.session_state["artifact"] = artifact

        except Exception as e:
            st.error(f"Script generation failed: {e}")


artifact = st.session_state.get("artifact")


# ---------------- Display + Validation ---------------- #

if artifact:

    st.subheader("Artifact Type")
    st.write(artifact.artifact_type)

    st.subheader("Name")
    st.write(artifact.name)

    st.subheader("Generated Script")
    st.code(artifact.script, language="javascript")

    try:
        issues = validate_script(artifact.script)

        if issues:
            st.warning(f"Issues detected: {issues}")
        else:
            st.success("No validation issues found")

    except Exception as e:
        st.warning(f"Validation skipped: {e}")


# ---------------- Deployment ---------------- #

    if st.button("Deploy to ServiceNow"):

        try:
            result = deploy_artifact(artifact.model_dump())

            st.subheader("Deployment Result")
            st.json(result)

        except Exception as e:
            st.error(f"Deployment failed: {e}")