# TCCA Bench

**Total Cost of Correct Answer — a benchmark for LLM agent memory systems.**

TCCA measures the one thing no existing benchmark captures: **correct answers per token spent across the entire pipeline** — retrieval, context loading, and generation combined.

## The Problem

Every memory benchmark measures recall: *did the agent remember the fact?* None measure efficiency: *how many tokens did it cost to remember?*

Systems report accuracy and token savings as separate numbers. ENGRAM: "95.5% token reduction AND +21.8pp accuracy." Mem0: accuracy, BLEU, F1, and token consumption as four metrics. Everyone knows efficiency matters. Nobody scores it.

Worse: existing metrics ignore **retrieval cost**. A system that uses a sub-agent to read 50 files (50K tokens) to select 3 files (3K tokens) looks efficient on context size — but spent 53K tokens total. A graph-based system that follows 3 edges (2K tokens) to find the same 3 files spent 5K total. No existing metric distinguishes these.

## The Metric

```
TCCA = answer_correct × (baseline_tokens / total_tokens)
```

Where:
- `total_tokens = retrieval_tokens + context_tokens + generation_tokens`
- `baseline_tokens` = cost of the naive strategy (load everything) on the same question

| TCCA | Meaning |
|------|---------|
| 0 | Wrong answer (no credit regardless of efficiency) |
| 1.0 | Same cost as loading everything |
| 5.0 | Correct answer at 1/5th the cost |
| < 1.0 | Net negative — spent MORE than loading everything |

**κ** = slope of TCCA over time as the knowledge base grows. κ > 0 means the system maintains efficiency as memory accumulates. κ < 0 means it degrades.

## The Benchmark

Built on [LongMemEval](https://github.com/xiaowu0162/LongMemEval) (500 questions, 6 types, ~40 sessions per question). Sessions are sliced into chronological steps. At each step:

1. **Store** — new sessions arrive
2. **Query** — ask the question
3. **Grade** — score the answer
4. **Log** — record TCCA with full token accounting

## The Adapter Interface

Any memory system implements two methods:

```python
from tcca_bench import MemoryAdapter, TokenUsage

class MyMemorySystem(MemoryAdapter):
    name = "my-system"

    def store(self, session_text: str, session_id: str, date: str) -> TokenUsage:
        # Ingest new knowledge. Return tokens spent on storage/management.
        ...

    def query(self, question: str) -> tuple[str, TokenUsage]:
        # Retrieve and answer. Return (answer, token_usage).
        ...

    def reset(self):
        # Reset for a new trial.
        ...
```

`TokenUsage` tracks three buckets:
- `retrieval_tokens` — search, re-ranking, sub-agent reasoning, graph traversal
- `context_tokens` — what gets loaded into the answer LLM prompt
- `generation_tokens` — answer LLM output

## Reference Adapters

| Adapter | Retrieval | LLM retrieval tokens | Description |
|---------|-----------|---------------------|-------------|
| `accumulate` | None | 0 | Load everything. TCCA = 1.0 baseline. |
| `bm25` | BM25 (lexical) | 0 | Programmatic keyword matching. No LLM calls for retrieval. |
| `vector-rag` | Embedding similarity | 0 | Semantic search via sentence-transformers. Embedding compute is CPU cost, not LLM tokens. |
| `llm-compression` | LLM-driven | >0 | Agent uses LLM to compress on store and reason about retrieval on query. |

Note: `retrieval_tokens` in TCCA tracks **LLM token spend** specifically — the cost of reasoning about what to retrieve. Programmatic systems (BM25, vector search) have real compute costs (CPU, embedding inference) but zero LLM token cost, which TCCA reflects. This is by design: TCCA measures the cost of the *intelligence* in the retrieval pipeline, not the infrastructure.

## Quick Start

```bash
# Clone
git clone https://github.com/turlockmike/tcca-bench.git
cd tcca-bench

# Install
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Download LongMemEval data (place in data/)
# See: https://github.com/xiaowu0162/LongMemEval

# Prepare trials (slice sessions into steps)
python scripts/prep_data.py \
  --dataset data/longmemeval_s_cleaned.json \
  --output data/trials \
  --limit 10

# Run benchmark (requires Ollama with a model running)
python scripts/run_tcca.py \
  --trials data/trials \
  --adapters accumulate,bm25,vector-rag,llm-compression \
  --model gemma4:26b \
  --limit 5 \
  --output results/my_run
```

## Requirements

- Python 3.10+
- [Ollama](https://ollama.com) running locally with a chat model
- LongMemEval dataset (downloaded separately)

## Preliminary Results

From initial smoke tests with Gemma 4 26B:

| Adapter | Accuracy (post-answer) | Mean TCCA | Mean Total Tokens |
|---------|----------------------|-----------|-------------------|
| accumulate | 25% | 0.25 | 31,941 |
| bm25 | 100% | 2.1 | 14,535 |
| vector-rag | 38% | 0.38 | 14,234 |
| llm-compression | 75% | 0.75 | 28,435 |

BM25 dominates on single-session questions (zero retrieval cost + good accuracy). Multi-session and temporal-reasoning question types — where evidence is scattered across sessions — are where the interesting differentiation should emerge. Full results across all question types pending.

## Contributing

Bring your own memory system. Implement the adapter interface, run the benchmark, report your TCCA scores. PRs welcome for new adapters.

## License

MIT
