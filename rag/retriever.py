import json
import os

DATA_PATH = os.path.join(os.path.dirname(__file__), "knowledge_base.json")


def load_data():
    if not os.path.exists(DATA_PATH):
        return []

    with open(DATA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def retrieve_context(query, top_k=3):
    try:
        data = load_data()

        query = query.lower()

        scored = []

        for item in data:
            content = item["content"].lower()

            score = sum(1 for word in query.split() if word in content)

            if score > 0:
                scored.append((score, item["content"]))

        scored.sort(reverse=True)

        results = [item[1] for item in scored[:top_k]]

        if not results:
            return "No relevant context found."

        return "\n\n".join(results)

    except Exception as e:
        return f"RAG unavailable: {str(e)}"