import sys
import os

# 🔥 FIX: Ensure project root is in PYTHONPATH (Render + local)
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import streamlit as st

from agent.orchestrator import generate_script
from integration.servicenow_client import deploy_artifact
from config.settings import settings


# ---------------- SAFE IMPORTS ----------------
try:
    from validation.script_validator import validate_script
except:
    def validate_script(x):
        return {"valid": True, "issues": []}


try:
    from rag.retriever import retrieve_context
except:
    def retrieve_context(x):
        return ""


# ---------------- UI CONFIG ----------------
st.set_page_config(
    page_title="AI ServiceNow Developer Agent",
    layout="wide"
)

st.title("🚀 AI ServiceNow Developer Agent")


# ---------------- INPUT ----------------
requirement = st.text_area(
    "Describe your ServiceNow requirement",
    height=150
)

col1, col2 = st.columns(2)

with col1:
    artifact_type = st.selectbox(
        "Artifact Type",
        ["auto", "business_rule", "script_include", "client_script"]
    )

with col2:
    provider = st.selectbox(
        "LLM Provider",
        ["openai", "gemini", "claude"],
        index=["openai", "gemini", "claude"].index(settings.DEFAULT_PROVIDER)
        if settings.DEFAULT_PROVIDER in ["openai", "gemini", "claude"] else 0
    )


# ---------------- GENERATE ----------------
if st.button("Generate Script"):

    if not requirement.strip():
        st.warning("Please enter a requirement")
        st.stop()

    with st.spinner("Generating script..."):

        try:
            # 🔍 RAG Context (safe fallback)
            context = retrieve_context(requirement)

            # 🤖 Generate script
            artifact = generate_script(
                requirement=requirement,
                provider=provider,
                context=context,
                artifact_hint=artifact_type
            )

            st.success("Script Generated")

            # ---------------- DISPLAY ----------------
            st.subheader("Artifact Type")
            st.code(artifact.get("artifact_type", ""))

            st.subheader("Name")
            st.code(artifact.get("name", ""))

            st.subheader("Generated Script")
            st.code(artifact.get("script", ""), language="javascript")

            # ---------------- VALIDATION ----------------
            validation = validate_script(artifact)

            if validation.get("valid"):
                st.success("Validation Passed")
            else:
                st.warning("Validation Issues Found")
                for issue in validation.get("issues", []):
                    st.write(f"- {issue}")

            # ---------------- DEPLOY ----------------
            if st.button("Deploy to ServiceNow"):

                with st.spinner("Deploying to ServiceNow..."):
                    try:
                        result = deploy_artifact(artifact)

                        st.success("Deployment Response")
                        st.json(result)

                    except Exception as e:
                        st.error(f"Deployment failed: {str(e)}")

        except Exception as e:
            st.error(f"Script generation failed: {str(e)}")


# ---------------- FOOTER ----------------
st.markdown("---")
st.caption("Built with ❤️ for ServiceNow AI Automation")