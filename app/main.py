import sys
import os

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import streamlit as st

from agent.orchestrator import generate_script
from integration.servicenow_client import deploy_artifact
from validation.script_validator import validate_script
from rag.retriever import retrieve_context


st.set_page_config(page_title="AI ServiceNow Developer Agent")

st.title("🚀 AI ServiceNow Developer Agent")


provider = st.selectbox(
    "Select AI Provider",
    ["openai", "gemini", "claude"],
    index=0  # ✅ default = openai
)


requirement = st.text_area(
    "Describe your ServiceNow requirement",
    height=150
)


if st.button("Show RAG Context"):

    try:
        context = retrieve_context(requirement)
        st.subheader("Retrieved Context")
        st.code(context)

    except Exception as e:
        st.error(f"RAG failed: {e}")


if st.button("Generate Script"):

    if not requirement.strip():
        st.warning("Please enter a requirement")
    else:
        try:
            artifact = generate_script(requirement, provider)
            st.session_state["artifact"] = artifact

        except Exception as e:
            st.error(f"Generation failed: {e}")


artifact = st.session_state.get("artifact")


if artifact:

    st.subheader("Artifact Type")
    st.write(artifact.artifact_type)

    st.subheader("Generated Script")
    st.code(artifact.script, language="javascript")

    issues = validate_script(artifact.script)

    if issues:
        st.warning(f"Issues detected: {issues}")

    if st.button("Deploy to ServiceNow"):

        try:
            result = deploy_artifact(artifact.model_dump())

            st.subheader("Deployment Result")
            st.json(result)

        except Exception as e:
            st.error(f"Deployment failed: {e}")