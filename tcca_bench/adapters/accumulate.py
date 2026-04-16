"""Accumulate adapter: load everything, no compression.

This is the baseline — TCCA = 1.0 by definition. Establishes baseline_tokens
for all other conditions at each step.
"""

import httpx

from tcca_bench.adapter import MemoryAdapter, TokenUsage

OLLAMA_URL = "http://localhost:11434"


class AccumulateAdapter(MemoryAdapter):
    name = "accumulate"

    def __init__(self, model: str = "gemma4:26b", ollama_url: str = OLLAMA_URL):
        self.model = model
        self.ollama_url = ollama_url
        self.sessions: list[str] = []

    def store(self, session_text: str, session_id: str, date: str) -> TokenUsage:
        self.sessions.append(session_text)
        return TokenUsage()  # zero cost — just append

    def query(self, question: str) -> tuple[str, TokenUsage]:
        # Load everything
        all_context = "\n\n".join(self.sessions)

        prompt = (
            "I will give you several history chats between you and a user. "
            "Please answer the question based on the relevant chat history.\n\n"
            f"History Chats:\n{all_context}\n\n"
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
            retrieval_tokens=0,
            context_tokens=prompt_tokens,
            generation_tokens=completion_tokens,
        )
        return answer, usage

    def reset(self):
        self.sessions = []
