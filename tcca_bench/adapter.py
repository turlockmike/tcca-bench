"""Adapter interface for LongMemEval-TCCA benchmark.

Any memory system implements two methods:
    store(session_text, session_id, date) → TokenUsage
    query(question) → (answer, TokenUsage)

The benchmark harness calls store() at each step (new knowledge arrives),
then query() (ask the question), then grades and computes TCCA.
"""

from dataclasses import dataclass, field


@dataclass
class TokenUsage:
    """Token accounting for a single operation."""
    retrieval_tokens: int = 0   # search, re-ranking, sub-agent reasoning, graph traversal
    context_tokens: int = 0     # what gets loaded into the answer LLM prompt
    generation_tokens: int = 0  # answer LLM output

    @property
    def total(self) -> int:
        return self.retrieval_tokens + self.context_tokens + self.generation_tokens

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            retrieval_tokens=self.retrieval_tokens + other.retrieval_tokens,
            context_tokens=self.context_tokens + other.context_tokens,
            generation_tokens=self.generation_tokens + other.generation_tokens,
        )

    def to_dict(self) -> dict:
        return {
            "retrieval_tokens": self.retrieval_tokens,
            "context_tokens": self.context_tokens,
            "generation_tokens": self.generation_tokens,
            "total_tokens": self.total,
        }


class MemoryAdapter:
    """Base class for memory system adapters."""

    name: str = "base"

    def store(self, session_text: str, session_id: str, date: str) -> TokenUsage:
        """Ingest new knowledge. Returns tokens spent on storage/management."""
        raise NotImplementedError

    def query(self, question: str) -> tuple[str, TokenUsage]:
        """Retrieve relevant context and answer. Returns (answer, token_usage)."""
        raise NotImplementedError

    def reset(self):
        """Reset state for a new trial."""
        raise NotImplementedError


def compute_tcca(answer_correct: bool, total_tokens: int, baseline_tokens: int) -> float:
    """Compute TCCA for a single (question, step) pair.

    TCCA = answer_correct × (baseline_tokens / total_tokens)

    - TCCA = 1.0  → same cost as loading everything
    - TCCA > 1.0  → cheaper than loading everything
    - TCCA = 0    → wrong answer
    - TCCA < 1.0  → more expensive than loading everything (net negative)
    """
    if not answer_correct or total_tokens <= 0 or baseline_tokens <= 0:
        return 0.0
    return baseline_tokens / total_tokens
