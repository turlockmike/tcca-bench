"""BM25 adapter: programmatic retrieval, zero LLM retrieval cost.

Represents Camp 1 — cheap, no reasoning, quality ceiling on multi-session.
"""

import httpx
from rank_bm25 import BM25Okapi

from tcca_bench.adapter import MemoryAdapter, TokenUsage

OLLAMA_URL = "http://localhost:11434"


class BM25Adapter(MemoryAdapter):
    name = "bm25"

    def __init__(self, model: str = "gemma4:26b", topk: int = 5, ollama_url: str = OLLAMA_URL):
        self.model = model
        self.topk = topk
        self.ollama_url = ollama_url
        self.sessions: list[str] = []
        self.session_ids: list[str] = []

    def store(self, session_text: str, session_id: str, date: str) -> TokenUsage:
        self.sessions.append(session_text)
        self.session_ids.append(session_id)
        return TokenUsage()  # zero cost — just append

    def query(self, question: str) -> tuple[str, TokenUsage]:
        if not self.sessions:
            return "I don't know", TokenUsage()

        # BM25 retrieval — zero LLM tokens
        corpus = [s.split() for s in self.sessions]
        bm25 = BM25Okapi(corpus)
        scores = bm25.get_scores(question.split())

        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
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
            retrieval_tokens=0,  # BM25 is lexical — zero LLM cost
            context_tokens=prompt_tokens,
            generation_tokens=completion_tokens,
        )
        return answer, usage

    def reset(self):
        self.sessions = []
        self.session_ids = []
