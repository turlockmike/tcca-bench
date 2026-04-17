#!/usr/bin/env python3
"""Run longitudinal TCCA benchmark using the adapter interface.

Usage:
    python run_tcca.py --dataset data/longmemeval_s_cleaned.json \
                       --trials data/trials \
                       --adapters accumulate,bm25,vector-rag,llm-compression \
                       --model gemma4:26b \
                       --limit 5 \
                       --output results/tcca_run_001
"""

import argparse
import json
import time
from pathlib import Path

from tcca_bench.adapter import TokenUsage, compute_tcca
from tcca_bench.eval_step import grade

OLLAMA_URL = "http://localhost:11434"


def load_adapter(name: str, model: str, work_dir: Path):
    """Load a memory adapter by name."""
    if name == "accumulate":
        from tcca_bench.adapters.accumulate import AccumulateAdapter
        return AccumulateAdapter(model=model, ollama_url=OLLAMA_URL)
    elif name == "bm25":
        from tcca_bench.adapters.bm25_adapter import BM25Adapter
        return BM25Adapter(model=model, topk=5, ollama_url=OLLAMA_URL)
    elif name == "vector-rag":
        from tcca_bench.adapters.vector_rag import VectorRAGAdapter
        return VectorRAGAdapter(model=model, topk=5, ollama_url=OLLAMA_URL)
    elif name == "llm-compression":
        from tcca_bench.adapters.llm_compression import LLMCompressionAdapter
        return LLMCompressionAdapter(model=model, work_dir=str(work_dir), ollama_url=OLLAMA_URL)
    elif name == "memfs":
        from tcca_bench.adapters.memfs_adapter import MemfsAdapter
        return MemfsAdapter(model=model, topk=5, ollama_url=OLLAMA_URL)
    elif name == "memfs-graph":
        # memfs with one-hop :LINK expansion enabled (graph-hypothesis mode).
        from tcca_bench.adapters.memfs_adapter import MemfsAdapter
        return MemfsAdapter(model=model, topk=5, expand_hops=1, ollama_url=OLLAMA_URL)
    else:
        raise ValueError(f"Unknown adapter: {name}")


def sessions_to_text(sessions: list[list[dict]], date: str = "") -> str:
    """Convert a LongMemEval session (list of turns) to text."""
    lines = []
    if date:
        lines.append(f"[{date}]")
    for turn in sessions:
        role = turn["role"].capitalize()
        lines.append(f"{role}: {turn['content']}")
    return "\n".join(lines)


def run_trial(
    trial_dir: Path,
    adapter_name: str,
    model: str,
    eval_model: str,
    output_dir: Path,
    baseline_cache: dict,
):
    """Run one trial (one question) with one adapter across all steps."""
    meta = json.loads((trial_dir / "meta.json").read_text())
    qid = meta["question_id"]
    question = meta["question"]
    answer = meta["answer"]
    question_type = meta["question_type"]
    answer_step = meta["answer_step"]
    n_steps = meta["n_steps"]

    results_file = output_dir / "results.jsonl"
    results_file.parent.mkdir(parents=True, exist_ok=True)

    # Resume: skip completed steps
    completed_steps = set()
    if results_file.exists():
        with open(results_file) as f:
            for line in f:
                if line.strip():
                    completed_steps.add(json.loads(line)["step"])

    # Load adapter
    work_dir = output_dir / "kb"
    adapter = load_adapter(adapter_name, model, work_dir)

    # We need to replay stores for completed steps to rebuild state
    if completed_steps:
        for step in range(1, max(completed_steps) + 1):
            step_dir = trial_dir / "steps" / f"step_{step:02d}"
            if step_dir.exists():
                for f in sorted(step_dir.glob("*.md")):
                    text = f.read_text()
                    adapter.store(text, f.stem, "")

    print(f"  Trial {qid} | {adapter_name} | {n_steps} steps | answer@{answer_step}")

    cumulative_store_tokens = TokenUsage()

    with open(results_file, "a") as out:
        for step in range(1, n_steps + 1):
            if step in completed_steps:
                continue

            t0 = time.time()

            # STORE: ingest new sessions
            step_dir = trial_dir / "steps" / f"step_{step:02d}"
            step_store_tokens = TokenUsage()
            if step_dir.exists():
                for f in sorted(step_dir.glob("*.md")):
                    text = f.read_text()
                    sid = f.stem
                    store_usage = adapter.store(text, sid, "")
                    step_store_tokens = step_store_tokens + store_usage

            cumulative_store_tokens = cumulative_store_tokens + step_store_tokens

            # QUERY: ask the question
            try:
                answer_text, query_usage = adapter.query(question)
            except Exception as e:
                print(f"    Step {step}: query error: {e}")
                answer_text = ""
                query_usage = TokenUsage()

            elapsed = time.time() - t0

            # Total tokens = amortized store cost + query cost
            # Amortize: divide cumulative store tokens by step number
            amortized_store = TokenUsage(
                retrieval_tokens=cumulative_store_tokens.retrieval_tokens // step,
            )
            total_usage = amortized_store + query_usage

            # GRADE
            is_post_answer = step >= answer_step
            task_success = False
            if answer_text and answer_text.lower() not in ("i don't know", "i don't know."):
                try:
                    task_success = grade(eval_model, question, answer, answer_text, question_type, OLLAMA_URL)
                except Exception as e:
                    print(f"    Step {step}: grade error: {e}")

            # TCCA
            baseline_key = f"{qid}_{step}"
            baseline_tokens = baseline_cache.get(baseline_key, total_usage.total)
            tcca = compute_tcca(task_success, total_usage.total, baseline_tokens)

            result = {
                "step": step,
                "adapter": adapter_name,
                "question_id": qid,
                "question_type": question_type,
                "answer_step": answer_step,
                "is_post_answer": is_post_answer,
                "task_success": task_success,
                "tcca": round(tcca, 4),
                "retrieval_tokens": total_usage.retrieval_tokens,
                "context_tokens": total_usage.context_tokens,
                "generation_tokens": total_usage.generation_tokens,
                "total_tokens": total_usage.total,
                "baseline_tokens": baseline_tokens,
                "store_tokens_cumulative": cumulative_store_tokens.retrieval_tokens,
                "hypothesis": answer_text[:200] if answer_text else "",
                "elapsed": round(elapsed, 2),
            }

            out.write(json.dumps(result) + "\n")
            out.flush()

            status = "✓" if task_success else "✗"
            post = "POST" if is_post_answer else "pre"
            print(f"    Step {step}/{n_steps}: {status} TCCA={tcca:.1f} | total={total_usage.total:,} baseline={baseline_tokens:,} | {post} | {elapsed:.1f}s")

    return results_file


def run_baseline(trial_dir: Path, model: str, eval_model: str, output_dir: Path) -> dict:
    """Run accumulate baseline and cache token counts for TCCA normalization."""
    cache_file = output_dir / "baseline_cache.json"

    if cache_file.exists():
        cache = json.loads(cache_file.read_text())
        print(f"  Loaded baseline cache: {len(cache)} entries")
        return cache

    print("\n--- Running accumulate baseline for TCCA normalization ---")
    meta = json.loads((trial_dir / "meta.json").read_text())
    qid = meta["question_id"]

    from tcca_bench.adapters.accumulate import AccumulateAdapter
    adapter = AccumulateAdapter(model=model, ollama_url=OLLAMA_URL)

    cache = {}
    n_steps = meta["n_steps"]

    for step in range(1, n_steps + 1):
        step_dir = trial_dir / "steps" / f"step_{step:02d}"
        if step_dir.exists():
            for f in sorted(step_dir.glob("*.md")):
                adapter.store(f.read_text(), f.stem, "")

        try:
            _, usage = adapter.query(meta["question"])
            cache[f"{qid}_{step}"] = usage.total
        except Exception:
            cache[f"{qid}_{step}"] = 1  # avoid division by zero

        print(f"    Baseline step {step}/{n_steps}: {cache[f'{qid}_{step}']:,} tokens")

    return cache


def main():
    parser = argparse.ArgumentParser(description="LongMemEval-TCCA Benchmark")
    parser.add_argument("--trials", required=True, help="Trials directory")
    parser.add_argument("--adapters", default="accumulate,bm25,vector-rag,llm-compression")
    parser.add_argument("--model", default="gemma4:26b")
    parser.add_argument("--eval-model", default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--output", default="results/tcca_run")
    args = parser.parse_args()

    eval_model = args.eval_model or args.model
    trials_dir = Path(args.trials)
    output_dir = Path(args.output)
    adapter_names = args.adapters.split(",")

    # Load manifest
    manifest = json.loads((trials_dir / "manifest.json").read_text())
    if args.limit > 0:
        manifest = manifest[: args.limit]

    print(f"\n{'#'*60}")
    print(f"# LongMemEval-TCCA Benchmark")
    print(f"# Trials:   {len(manifest)}")
    print(f"# Adapters: {adapter_names}")
    print(f"# Model:    {args.model}")
    print(f"# Output:   {output_dir}")
    print(f"{'#'*60}")

    # Save config
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "config.json").write_text(json.dumps({
        "adapters": adapter_names,
        "model": args.model,
        "eval_model": eval_model,
        "n_trials": len(manifest),
        "trial_ids": [m["question_id"] for m in manifest],
    }, indent=2))

    # Load or initialize baseline cache
    baseline_cache_path = output_dir / "baseline_cache.json"
    baseline_cache = {}
    if baseline_cache_path.exists():
        baseline_cache = json.loads(baseline_cache_path.read_text())

    # For each trial, run baseline first, then all adapters
    for i, trial_meta in enumerate(manifest):
        qid = trial_meta["question_id"]
        trial_dir = trials_dir / qid

        print(f"\n{'='*60}")
        print(f"Trial {i+1}/{len(manifest)}: {qid} ({trial_meta['question_type']})")
        print(f"{'='*60}")

        # Run baseline for THIS trial if not cached
        if f"{qid}_1" not in baseline_cache:
            trial_baseline = run_baseline(trial_dir, args.model, eval_model, output_dir)
            baseline_cache.update(trial_baseline)
            baseline_cache_path.write_text(json.dumps(baseline_cache, indent=2))
        else:
            print(f"  Baseline cached for {qid}")

        # Run each adapter
        for adapter_name in adapter_names:
            trial_output = output_dir / adapter_name / qid
            run_trial(trial_dir, adapter_name, args.model, eval_model, trial_output, baseline_cache)

    # Report
    print(f"\n{'='*60}")
    print("Generating TCCA report...")
    print(f"{'='*60}")

    # Aggregate results across all adapters and trials
    summary = {}
    for adapter_name in adapter_names:
        records = []
        adapter_dir = output_dir / adapter_name
        if adapter_dir.exists():
            for trial_dir in sorted(adapter_dir.iterdir()):
                rf = trial_dir / "results.jsonl"
                if rf.exists():
                    with open(rf) as f:
                        for line in f:
                            if line.strip():
                                records.append(json.loads(line))

        if not records:
            continue

        post_answer = [r for r in records if r.get("is_post_answer")]
        successes = sum(1 for r in post_answer if r["task_success"])
        tcca_values = [r["tcca"] for r in post_answer]
        total_tokens_values = [r["total_tokens"] for r in post_answer]

        summary[adapter_name] = {
            "n_records": len(records),
            "n_post_answer": len(post_answer),
            "accuracy": round(successes / len(post_answer), 4) if post_answer else 0,
            "mean_tcca": round(sum(tcca_values) / len(tcca_values), 4) if tcca_values else 0,
            "mean_total_tokens": round(sum(total_tokens_values) / len(total_tokens_values)) if total_tokens_values else 0,
        }

    # Print comparison
    print(f"\n  {'Adapter':<20} {'Accuracy':>10} {'Mean TCCA':>12} {'Mean Tokens':>14}")
    print(f"  {'-'*56}")
    for name, s in summary.items():
        print(f"  {name:<20} {s['accuracy']:>9.1%} {s['mean_tcca']:>12.2f} {s['mean_total_tokens']:>14,}")

    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"\nResults in: {output_dir}")


if __name__ == "__main__":
    main()
