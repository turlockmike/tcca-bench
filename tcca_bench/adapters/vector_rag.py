"""Vector RAG adapter: embedding-based retrieval, zero LLM retrieval cost.

Represents the industry default — semantic search via embeddings.
Uses sentence-transformers for local embedding (no API calls).
"""

import numpy as np
import httpx
from sentence_transformers import SentenceTransformer

from tcca_bench.adapter import MemoryAdapter, TokenUsage

OLLAMA_URL = "http://localhost:11434"
# all-MiniLM-L6-v2: fast, small (80MB), decent quality. Runs on CPU.
EMBEDDING_MODEL = "all-MiniLM-L6-v2"


class VectorRAGAdapter(MemoryAdapter):
    name = "vector-rag"

    def __init__(
        self,
        model: str = "gemma4:26b",
        topk: int = 5,
        ollama_url: str = OLLAMA_URL,
        embedding_model: str = EMBEDDING_MODEL,
    ):
        self.model = model
        self.topk = topk
        self.ollama_url = ollama_url
        self.embedder = SentenceTransformer(embedding_model)
        self.sessions: list[str] = []
        self.embeddings: list[np.ndarray] = []

    def store(self, session_text: str, session_id: str, date: str) -> TokenUsage:
        self.sessions.append(session_text)
        # Embed on store — this is a programmatic cost (not LLM tokens)
        embedding = self.embedder.encode(session_text, convert_to_numpy=True)
        self.embeddings.append(embedding)
        return TokenUsage()  # embedding compute is not LLM tokens

    def query(self, question: str) -> tuple[str, TokenUsage]:
        if not self.sessions:
            return "I don't know", TokenUsage()

        # Semantic search — zero LLM tokens
        query_embedding = self.embedder.encode(question, convert_to_numpy=True)
        similarities = [
            np.dot(query_embedding, emb) / (np.linalg.norm(query_embedding) * np.linalg.norm(emb))
            for emb in self.embeddings
        ]

        ranked = sorted(range(len(similarities)), key=lambda i: similarities[i], reverse=True)
        top_indices = sorted(ranked[: self.topk])  # chronological order

        context = "\n\n".join(self.sessions[i] for i in top_indices)

        prompt = (
            "I will give you several history chats between you and a user. "
            "Please answer the question based on the relevant chat history.\n\n"
            f"History Chats:\n{context}\n\n"
            f"Question: {question}\n"
            "Answer concisely:"
        )

        response = httpx.post(
            f"{self.ollama_url}/api/chat",
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "think": False,
                "options": {"temperature": 0, "num_predict": 500},
            },
            timeout=300.0,
        )
        response.raise_for_status()
        data = response.json()

        answer = data.get("message", {}).get("content", "").strip()
        prompt_tokens = data.get("prompt_eval_count", 0)
        completion_tokens = data.get("eval_count", 0)

        usage = TokenUsage(
            retrieval_tokens=0,  # embedding search is not LLM tokens
            context_tokens=prompt_tokens,
            generation_tokens=completion_tokens,
        )
        return answer, usage

    def reset(self):
        self.sessions = []
        self.embeddings = []
