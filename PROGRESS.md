# TCCA Bench — Progress & Handoff Notes

**Last updated:** 2026-04-17

This document captures the current state of the TCCA benchmark so another developer can pick it up. Read [README.md](./README.md) first for the concept and metric; this file is about **what's built, what works, what doesn't, and what to do next.**

---

## Status in One Sentence

The benchmark harness runs end-to-end on two datasets (LongMemEval and BEAM) with four adapters, but the LLM-compression adapter is failing catastrophically at BEAM scale — the benchmark works; the compression implementation doesn't yet.

---

## What's Built

### Core Library (`tcca_bench/`)

| File | Status | Purpose |
|------|--------|---------|
| `adapter.py` | ✅ stable | `MemoryAdapter` interface, `TokenUsage` dataclass, `compute_tcca()` formula |
| `adapters/accumulate.py` | ✅ stable | Baseline — load everything into context |
| `adapters/bm25_adapter.py` | ✅ stable | Programmatic BM25 retrieval, top-K sessions |
| `adapters/vector_rag.py` | ✅ stable | Sentence-transformer embedding similarity |
| `adapters/llm_compression.py` | ⚠️ broken at scale | Extracts facts at store-time, answers from compressed index |
| `eval_step.py` | ✅ stable | Ollama-based answer grading (think:false for clean yes/no) |
| `retrieve.py` | ✅ stable | Standalone BM25 over a file-based KB |

### Scripts (`scripts/`)

| File | Status | Purpose |
|------|--------|---------|
| `prep_data.py` | ✅ works | Slices LongMemEval into chronological step directories |
| `prep_beam.py` | ✅ works | Slices BEAM conversations (1M+ tokens) into steps, extracts 20 questions per trial across 10 types |
| `run_tcca.py` | ✅ works | Runner for LongMemEval-format data |
| `run_beam.py` | ✅ works | Runner for BEAM-format data (multiple questions per trial) |
| `cq-report.py` | ✅ works | Aggregation / kappa analysis |

### Supporting

- `curator.md` — system prompt for the LLM compression agent (currently too generic, see issues below)
- `requirements.txt` — httpx, rank-bm25, tiktoken, sentence-transformers, datasets
- Data lives in `data/` (gitignored); results in `results/` (gitignored)

---

## Results So Far

### LongMemEval smoke test (2 trials, 4 adapters)

Single-session-user questions, ~115K tokens per conversation. Accumulate fits in Gemma 4 26B's 32K window after truncation.

| Adapter | Accuracy (post-answer) | Mean TCCA | Mean tokens |
|---------|-----------------------|-----------|-------------|
| accumulate | 25% | 0.25 | 31,941 |
| bm25 | 100% | 2.1 | 14,535 |
| vector-rag | 38% | 0.38 | 14,234 |
| llm-compression | 75% | 0.75 | 28,435 |

**Takeaways:**
- BM25 dominates on single-session-user questions. Expected — keyword match on unique terms wins.
- Accumulate degrades as context grows past ~32K (attention dilution + truncation).
- LLM-compression gets more answers right than accumulate but costs too many tokens to be TCCA-positive.
- **Known bug in this run:** baseline cache didn't properly track per-trial baselines; trial 2 got TCCA=1.0 for everything. Fixed in code, needs a clean re-run.

### BEAM smoke test (1 trial, 2 adapters, 3 questions × 10 steps)

1M tokens per conversation, 10 question types including contradiction_resolution, multi_session_reasoning, temporal_reasoning.

| Adapter | Correct | Mean TCCA | Mean tokens |
|---------|---------|-----------|-------------|
| bm25 | 20/30 (67%) | 0.67 | 31,730 |
| llm-compression | 0/30 (0%) | 0.00 | 1,503 |

By question type (limited sample — only abstention and contradiction_resolution tested):

| Type | BM25 Acc | BM25 TCCA | LLM-comp Acc |
|------|----------|-----------|--------------|
| abstention | 95% | 0.95 | 0% |
| contradiction_resolution | 10% | 0.10 | 0% |

**Takeaways:**
- **BM25's quality ceiling showed up.** 10% on contradiction_resolution — BM25 can't synthesize contradictory facts across sessions. This is the regime where the hypothesis predicted compression should win. It doesn't, yet.
- **LLM-compression is broken at BEAM scale.** 0/30 correct. The store-time fact extraction produces something (587–3,022 tokens at query time) but it's not usable for answering.
- **32K model context is masking the accumulate failure.** Both accumulate and BM25 cap out at 32K (baseline truncates from 1M; BM25 selects ~15K). TCCA can't distinguish them at this scale. A larger-context model would show accumulate's real cost.

---

## Known Issues

### 1. LLM-compression adapter fails at BEAM scale (CRITICAL)

**Symptom:** 0% accuracy on all 30 BEAM questions. Extracted facts appear in the index (587–3,022 tokens) but don't support answering.

**Hypothesis about cause:**
- The store prompt asks for "all key facts" as a bulleted list, but BEAM sessions are long, multi-topic technical discussions. A single bulleted summary loses relational structure (what-was-said-when, what-contradicts-what).
- Indexing by `session_id` only doesn't capture entities, topics, or temporal order.
- The answer prompt dumps all compressed facts as context — no retrieval, no reasoning about which session matters.

**File:** `tcca_bench/adapters/llm_compression.py`

**What to try:**
- Structured extraction (entities, facts-with-timestamps, contradictions) instead of bulleted lists
- Two-phase retrieval: select relevant sessions by topic/entity, then load their detailed facts
- Preserve temporal order explicitly (BEAM's `time_anchor` field is in the raw data but ignored by current adapter)
- Graph-based indexing (entity → facts → supporting session)

### 2. Small context model caps TCCA differentiation

**Symptom:** Both accumulate and BM25 hit Gemma 4 26B's 32K context window. TCCA baselines are truncated, so ratios don't reflect the true 1M accumulate cost.

**Fix:** Test with a larger-context model (Gemma 3 at 128K via different Ollama deploy, or cloud API with 200K+).

### 3. Baseline caching had per-trial bugs in LongMemEval runs

**Symptom:** LongMemEval trial 2 got TCCA=1.0 for every adapter because baseline_tokens matched total_tokens.

**Status:** Fixed in `run_tcca.py` — each trial now correctly computes its own baseline and appends to the shared cache. Not re-run yet.

### 4. Abstention skews BEAM results

**Symptom:** 95% accuracy on abstention for BM25 is misleading — "I don't know" is the correct answer and aggressive compression/retrieval failures both produce "I don't know."

**Fix:** When reporting, separate abstention from other types. A high abstention score on an adapter that's wrong everywhere else isn't a win.

---

## How to Run

### Setup

```bash
git clone https://github.com/turlockmike/tcca-bench.git
cd tcca-bench
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Ollama

Requires Ollama running locally with a chat model. Default assumes `gemma4:26b`.

```bash
ollama pull gemma4:26b  # or whatever model you want to test
```

### LongMemEval run

```bash
# Download LongMemEval data first (not bundled — too large).
# See: https://github.com/xiaowu0162/LongMemEval
# Place as: data/longmemeval_s_cleaned.json

python scripts/prep_data.py \
  --dataset data/longmemeval_s_cleaned.json \
  --output data/trials \
  --limit 10

python scripts/run_tcca.py \
  --trials data/trials \
  --adapters accumulate,bm25,vector-rag,llm-compression \
  --model gemma4:26b \
  --limit 5 \
  --output results/run_001
```

### BEAM run

```bash
# BEAM auto-downloads from HuggingFace on first use
python scripts/prep_beam.py \
  --split 1M \
  --step-size 1 \
  --output data/beam_trials \
  --limit 3

python scripts/run_beam.py \
  --trials data/beam_trials \
  --adapters accumulate,bm25,vector-rag,llm-compression \
  --model gemma4:26b \
  --limit 1 \
  --questions-per-step 3 \
  --output results/beam_run
```

### Timing at BEAM scale (1M token conversations)

- Accumulate baseline is the bottleneck: ~2 min/query × ~20 questions × 10 steps ≈ **~6 hours per trial**
- BM25 and vector-rag: ~30s/query, ~10 min per adapter per trial
- LLM-compression: store phase is ~5 min per step, query phase is fast
- Full 4-adapter run on 1 BEAM trial: **~10 hours with caching**

Use `--questions-per-step 3` for smoke tests.

---

## What to Work On Next

Ordered by impact:

### 1. Fix the LLM-compression adapter (CRITICAL)

The current adapter loses too much information at store time. Options:

- **Structured extraction:** extract entities, facts-with-dates, contradictions as separate fields instead of a bulleted blob
- **Two-phase retrieval at query time:** select relevant compressed sessions first, then read their details (requires keeping more detail at store time)
- **Graph memory:** build entity → fact → session edges, use RAG to find entry point + graph traversal for depth
- **Use `infer` as the compression agent** (planned but not wired up): let the agent reason about what to keep/merge/connect using its bash tool

Target: **Beat BM25 on contradiction_resolution** (BM25 currently at 10%). If compression can't beat 10% on contradictions, the hypothesis is in trouble.

### 2. Run the full BEAM benchmark across all 4 adapters

Requires fixing issue #1 first. Then:

```bash
python scripts/run_beam.py \
  --trials data/beam_trials \
  --adapters accumulate,bm25,vector-rag,llm-compression \
  --model <128K-context model> \
  --limit 5 \
  --output results/beam_full
```

All 20 questions per trial, 5 trials, larger-context model. This is the real benchmark data.

### 3. Add token ledger for larger-context models

Current Ollama setup gives Gemma 4 26B at 32K context. To test TCCA at real 1M scale, need:
- Gemma 3 27B at 128K (Ollama supports this with longer context configs)
- Or cloud API adapter (OpenAI/Anthropic) for 200K+ context
- Or model-specific tokenizer paths for exact baseline counting

### 4. Add a third-party adapter

Prove the interface works for something that isn't ours. Candidates:
- **Mem0** — has a clean Python API. Wrap `mem0.Memory.add()` and `mem0.Memory.search()` in the adapter interface.
- **Letta/MemGPT** — more involved but well-documented.
- **Zep** — REST API, good for showing the interface handles remote services.

### 5. Held-out test set for ratchet optimization

Questions 1-400 for training (mutating curator prompt), questions 401-500 for evaluation. Not implemented — would need question partitioning in `run_beam.py` and a ratchet loop script.

---

## Architecture Decisions (and Why)

### Why `store()` / `query()` as the interface

- Maps to how real memory systems work (Mem0, Letta, Zep all have similar APIs)
- Forces clean accounting: retrieval_tokens must be attributed to the right phase
- Allows pre-computation at store time (compression is amortized)

### Why TCCA = answer_correct × (baseline / total)

- Previous metric (CQ) had N-growth inflation bug — any constant-K strategy got automatic κ > 0
- CES fixed N-growth but ignored retrieval cost — a sub-agent reading 50 files looked as efficient as a graph hop
- TCCA captures total pipeline cost normalized by the naive strategy
- Baseline normalization handles per-question difficulty (hard questions need more context)

### Why Ollama native API (not OpenAI-compatible)

- `think:false` parameter only works on native `/api/chat`, not `/v1/chat/completions`
- Gemma 4 emits reasoning tokens to a separate field that OpenAI API hides
- We need exact `prompt_eval_count` and `eval_count` for TCCA accounting

### Why BEAM over LongMemEval as the primary dataset

- LongMemEval is 115K tokens per conversation — BM25 brute-forces it
- BEAM is 1M tokens — attention dilution and retrieval failure become real
- BEAM has contradiction_resolution and multi_session_reasoning — the question types where compression should matter
- Both kept: LongMemEval for fast iteration, BEAM for real tests

---

## Research Context

- [The Compression Hypothesis](https://docs.google.com/document/d/1Crg1aWYohatj2SvP4Ec-1-_norNLUGmu1iYCv2X0pjw/edit?tab=t.oq4nt0h0iypg) — the paper this benchmark was built to test
- Three metric iterations documented: CQ (N-growth bug), CES (ignored retrieval cost), TCCA (captures full pipeline cost)
- Karpathy agent review identified the retrieval-cost blind spot in CES that led to TCCA
- Pike review shaped the adapter interface naming and file structure

---

## Quick Questions You'll Have

**Q: Why does llm-compression have `store_tokens=33,268` on BEAM but the query only loads `mean tokens=1,503`?**

A: The compression is aggressive — each 100K-token session compresses down to ~300 tokens of extracted facts. At query time, the adapter loads all cumulative facts (~1.5K tokens for early steps, ~3K for later steps). The store cost is the investment; the query cost is the payoff. Whether the payoff is worth it is what TCCA measures — currently 0% accuracy means TCCA=0 regardless of how efficient the query was.

**Q: Is `work_dir` used for anything in llm_compression?**

A: It's passed but not currently written to. The adapter uses an in-memory `self.index` dict. The `work_dir` is for a future version that uses the filesystem as the KB (what `infer` would do via bash). Not wired up yet.

**Q: Why aren't we using `infer` yet?**

A: The smoke tests call Ollama directly for precise token accounting. `infer` would add another layer of process/parsing that we haven't needed for the baseline adapters. The plan was: get Ollama-direct working, then wire `infer` in as a more capable compression adapter. Ollama-direct works; `infer` is the next architecture iteration.

**Q: Can I run this without Ollama?**

A: Not currently — every adapter hard-codes Ollama's `/api/chat` endpoint. To support OpenAI/Anthropic/etc., refactor the LLM client into a shared `tcca_bench/llm.py` that any adapter can use. The token accounting (`prompt_eval_count` / `eval_count`) would need provider-specific extraction since each API returns usage differently.

---

## Contact / Questions

Repo owner: @turlockmike
