from sentence_transformers import SentenceTransformer
from rag.vector_store import client, COLLECTION, ensure_collection

model = SentenceTransformer("all-MiniLM-L6-v2")


def retrieve_context(query, top_k=3):

    ensure_collection()

    query_vector = model.encode(query).tolist()

    try:
        results = client.search(
            collection_name=COLLECTION,
            query_vector=query_vector,
            limit=top_k,
        )
    except Exception:
        return "No RAG data available."

    if not results:
        return "No relevant context found."

    return "\n\n".join([r.payload.get("content", "") for r in results])