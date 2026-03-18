from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, Distance

COLLECTION = "servicenow_scripts"

client = QdrantClient(":memory:")


def ensure_collection(vector_size=384):
    collections = [c.name for c in client.get_collections().collections]

    if COLLECTION not in collections:
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        )