import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sentence_transformers import SentenceTransformer
from rag.vector_store import client, COLLECTION, ensure_collection

model = SentenceTransformer("all-MiniLM-L6-v2")


def ingest_sample():

    ensure_collection()

    sample_data = [
        "Use GlideRecord to query ServiceNow tables.",
        "Business Rules run on insert, update, delete.",
        "Script Includes are reusable server-side classes.",
    ]

    vectors = model.encode(sample_data).tolist()

    points = []

    for i, (text, vector) in enumerate(zip(sample_data, vectors)):
        points.append({
            "id": i,
            "vector": vector,
            "payload": {"content": text}
        })

    client.upsert(collection_name=COLLECTION, points=points)

    print("RAG initialized with sample data.")


if __name__ == "__main__":
    ingest_sample()