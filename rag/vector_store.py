from qdrant_client import QdrantClient

# Use in-memory DB for Render (no disk persistence)
client = QdrantClient(":memory:")

COLLECTION = "servicenow_scripts"