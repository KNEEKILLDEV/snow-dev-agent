from rag.vector_store import client, COLLECTION, ensure_collection

# Lazy model loading
_model = None


def get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def retrieve_context(query, top_k=3):
    try:
        ensure_collection()

        model = get_model()
        query_vector = model.encode(query).tolist()

        results = client.search(
            collection_name=COLLECTION,
            query_vector=query_vector,
            limit=top_k,
        )

        if not results:
            return "No relevant context found."

        return "\n\n".join([r.payload.get("content", "") for r in results])

    except Exception as e:
        return f"RAG unavailable: {str(e)}"