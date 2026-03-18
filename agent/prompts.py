def build_prompt(requirement: str, context: str) -> str:
    """
    Builds structured prompt for LLM
    """

    return f"""
You are a ServiceNow expert developer.

Use the context below to generate a valid ServiceNow script.

### Context:
{context}

### Requirement:
{requirement}

### Instructions:
- Identify correct artifact type (business_rule / script_include / client_script)
- Generate clean, production-ready ServiceNow script
- Follow best practices (GlideRecord, try/catch, logging)

### Output Format (STRICT JSON ONLY):
{{
  "artifact_type": "business_rule | script_include | client_script",
  "name": "Name of the artifact",
  "table": "incident",
  "when": "before | after",
  "insert": true,
  "update": true,
  "script": "FULL SCRIPT HERE"
}}
"""