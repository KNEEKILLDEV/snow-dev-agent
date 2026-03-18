from rag.vector_store import client, COLLECTION, ensure_collection


def ingest_sample():
    try:
        ensure_collection()

        sample_data = [
            "Use GlideRecord to query ServiceNow tables.",
            "Business Rules execute on insert, update, delete.",
            "Script Includes are reusable server-side classes.",
        ]

        # Minimal lightweight embeddings (no model needed here)
        for i, text in enumerate(sample_data):
            client.upsert(
                collection_name=COLLECTION,
                points=[
                    {
                        "id": i,
                        "vector": [0.1] * 384,  # dummy vector (fast + safe)
                        "payload": {"content": text},
                    }
                ],
            )

    except Exception:
        pass