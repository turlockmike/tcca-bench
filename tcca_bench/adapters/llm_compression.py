"""LLM Compression adapter: agent-driven storage management + retrieval.

Represents Camp 2 — the agent actively compresses its knowledge base on store(),
then uses its compressed KB to answer on query(). Both operations cost LLM tokens.

Uses Ollama directly (not infer) for precise token accounting.
"""

import httpx
import os
from pathlib import Path

from tcca_bench.adapter import MemoryAdapter, TokenUsage

OLLAMA_URL = "http://localhost:11434"

CURATOR_SYSTEM = """You manage a knowledge base of conversation sessions stored as files.

Your goals:
1. Keep the knowledge base small and well-organized
2. Preserve ALL factual information — dates, names, preferences, decisions, specific facts
3. Remove noise — greetings, filler, generic advice that isn't personalized
4. Maintain an INDEX listing key topics and where to find them

Be aggressive about compression. Every token costs attention when you need to find something later."""

ANSWER_SYSTEM = """You answer questions using ONLY the provided context. If the information is not in the context, say "I don't know". Answer concisely."""


class LLMCompressionAdapter(MemoryAdapter):
    name = "llm-compression"

    def __init__(
        self,
        model: str = "gemma4:26b",
        work_dir: str = "/tmp/llm-compression-kb",
        ollama_url: str = OLLAMA_URL,
    ):
        self.model = model
        self.ollama_url = ollama_url
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.index: dict[str, str] = {}  # topic -> facts
        self.raw_sessions: list[str] = []
        self.session_count = 0
        self._store_tokens = TokenUsage()  # accumulate store costs for amortization

    def _call_llm(self, system: str, user: str, max_tokens: int = 1000) -> tuple[str, int, int]:
        """Call Ollama and return (response, prompt_tokens, completion_tokens)."""
        response = httpx.post(
            f"{self.ollama_url}/api/chat",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                "think": False,
                "options": {"temperature": 0, "num_predict": max_tokens},
            },
            timeout=300.0,
        )
        response.raise_for_status()
        data = response.json()
        text = data.get("message", {}).get("content", "").strip()
        prompt_tok = data.get("prompt_eval_count", 0)
        completion_tok = data.get("eval_count", 0)
        return text, prompt_tok, completion_tok

    def store(self, session_text: str, session_id: str, date: str) -> TokenUsage:
        """Ingest and compress: extract key facts from the session."""
        self.session_count += 1

        # Ask the LLM to extract key facts from this session
        prompt = (
            f"Extract ALL key facts from this conversation session. "
            f"Include: names, dates, numbers, preferences, decisions, specific details.\n"
            f"Output ONLY the facts as a bulleted list. No commentary.\n\n"
            f"Session ({date}):\n{session_text}"
        )

        facts_text, prompt_tok, completion_tok = self._call_llm(
            CURATOR_SYSTEM, prompt, max_tokens=500
        )

        usage = TokenUsage(
            retrieval_tokens=prompt_tok + completion_tok,
        )
        self._store_tokens = self._store_tokens + usage

        # Store the compressed facts
        if facts_text and facts_text.strip() != "No key facts found.":
            self.index[session_id] = f"[{date}] {facts_text}"

        return usage

    def query(self, question: str) -> tuple[str, TokenUsage]:
        """Answer from compressed KB. Two-phase: select relevant facts, then answer."""
        if not self.index:
            return "I don't know", TokenUsage()

        # Phase 1: Build context from compressed facts
        # For small KBs, load all facts (they're already compressed)
        all_facts = "\n\n".join(
            f"--- {sid} ---\n{facts}" for sid, facts in self.index.items()
        )

        # If compressed KB is small enough, skip retrieval and just load it all
        # This is the compression payoff: the KB is small enough to load entirely
        context = all_facts

        # Phase 2: Answer the question
        prompt = f"Context:\n{context}\n\nQuestion: {question}\nAnswer concisely:"

        answer, prompt_tok, completion_tok = self._call_llm(
            ANSWER_SYSTEM, prompt, max_tokens=500
        )

        usage = TokenUsage(
            retrieval_tokens=0,  # facts already compressed at store time
            context_tokens=prompt_tok,
            generation_tokens=completion_tok,
        )

        return answer, usage

    def reset(self):
        self.index = {}
        self.raw_sessions = []
        self.session_count = 0
        self._store_tokens = TokenUsage()

    def get_store_tokens(self) -> TokenUsage:
        """Get accumulated store-time token usage (for amortized TCCA)."""
        return self._store_tokens
