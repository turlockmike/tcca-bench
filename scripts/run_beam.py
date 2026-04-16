#!/usr/bin/env python3
"""Run TCCA benchmark on BEAM dataset.

BEAM has ~1M tokens per conversation, 10 sessions, 20 questions per trial.
At each step (session), we store the new session and then ask ALL 20 questions.

Usage:
    python scripts/run_beam.py \
        --trials data/beam_trials \
        --adapters accumulate,bm25,vector-rag,llm-compression \
        --model gemma4:26b \
        --limit 1 \
        --output results/beam_run
"""

import argparse
import json
import sys
import time
from pathlib import Path

# Add parent dir to path for tcca_bench imports
sys.path.insert(0, str(Path(__file__).parent.parent))

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
    else:
        raise ValueError(f"Unknown adapter: {name}")


def run_step_questions(
    adapter,
    questions: list[dict],
    step: int,
    eval_model: str,
    baseline_cache: dict,
    conv_id: str,
) -> list[dict]:
    """Ask all questions at this step, grade, compute TCCA. Returns list of result dicts."""
    results = []
    for q in questions:
        qid = q["question_id"]
        question = q["question"]
        answer = q["answer"]
        qtype = q["question_type"]

        t0 = time.time()
        try:
            hypothesis, query_usage = adapter.query(question)
        except Exception as e:
            hypothesis = ""
            query_usage = TokenUsage()
            print(f"        query error: {e}")

        elapsed = time.time() - t0

        # Grade
        task_success = False
        if hypothesis and hypothesis.lower() not in ("i don't know", "i don't know.", ""):
            try:
                task_success = grade(eval_model, question, answer, hypothesis, qtype, OLLAMA_URL)
            except Exception as e:
                print(f"        grade error: {e}")

        # TCCA
        total_tokens = query_usage.total
        baseline_key = f"{conv_id}_{step}_{qid}"
        baseline_tokens = baseline_cache.get(baseline_key, total_tokens or 1)
        tcca = compute_tcca(task_success, total_tokens, baseline_tokens)

        results.append({
            "step": step,
            "conversation_id": conv_id,
            "question_id": qid,
            "question_type": qtype,
            "task_success": task_success,
            "tcca": round(tcca, 4),
            "retrieval_tokens": query_usage.retrieval_tokens,
            "context_tokens": query_usage.context_tokens,
            "generation_tokens": query_usage.generation_tokens,
            "total_tokens": total_tokens,
            "baseline_tokens": baseline_tokens,
            "hypothesis": (hypothesis or "")[:200],
            "elapsed": round(elapsed, 2),
        })

    return results


def run_baseline_trial(trial_dir: Path, meta: dict, model: str) -> dict:
    """Run accumulate baseline for one trial, return {key: total_tokens} cache."""
    from tcca_bench.adapters.accumulate import AccumulateAdapter

    conv_id = meta["conversation_id"]
    questions = meta["questions"]
    n_steps = meta["n_steps"]
    adapter = AccumulateAdapter(model=model, ollama_url=OLLAMA_URL)

    cache = {}
    for step in range(1, n_steps + 1):
        step_dir = trial_dir / "steps" / f"step_{step:02d}"
        if step_dir.exists():
            for f in sorted(step_dir.glob("*.md")):
                adapter.store(f.read_text(), f.stem, "")

        # Ask each question and record baseline tokens
        for q in questions:
            qid = q["question_id"]
            try:
                _, usage = adapter.query(q["question"])
                cache[f"{conv_id}_{step}_{qid}"] = usage.total
            except Exception:
                cache[f"{conv_id}_{step}_{qid}"] = 1

        print(f"    Baseline step {step}/{n_steps}: ~{adapter.sessions and sum(len(s) for s in adapter.sessions) // 4:,} tokens accumulated")

    return cache


def run_trial(
    trial_dir: Path,
    meta: dict,
    adapter_name: str,
    model: str,
    eval_model: str,
    output_dir: Path,
    baseline_cache: dict,
):
    """Run one trial with one adapter across all steps."""
    conv_id = meta["conversation_id"]
    questions = meta["questions"]
    n_steps = meta["n_steps"]

    results_file = output_dir / "results.jsonl"
    results_file.parent.mkdir(parents=True, exist_ok=True)

    # Resume
    completed = set()
    if results_file.exists():
        with open(results_file) as f:
            for line in f:
                if line.strip():
                    r = json.loads(line)
                    completed.add((r["step"], r["question_id"]))

    # Load adapter
    work_dir = output_dir / "kb"
    adapter = load_adapter(adapter_name, model, work_dir)

    # Replay stores for completed steps
    max_completed_step = max((s for s, _ in completed), default=0)
    for step in range(1, max_completed_step + 1):
        step_dir = trial_dir / "steps" / f"step_{step:02d}"
        if step_dir.exists():
            for f in sorted(step_dir.glob("*.md")):
                adapter.store(f.read_text(), f.stem, "")

    print(f"  {adapter_name} | {n_steps} steps × {len(questions)} questions")

    with open(results_file, "a") as out:
        for step in range(1, n_steps + 1):
            # Check if all questions for this step are done
            step_questions_done = all((step, q["question_id"]) in completed for q in questions)
            if step_questions_done:
                continue

            # STORE
            step_dir = trial_dir / "steps" / f"step_{step:02d}"
            store_usage = TokenUsage()
            if step_dir.exists() and step > max_completed_step:
                for f in sorted(step_dir.glob("*.md")):
                    su = adapter.store(f.read_text(), f.stem, "")
                    store_usage = store_usage + su

            # QUERY all questions
            step_results = run_step_questions(
                adapter, questions, step, eval_model, baseline_cache, conv_id,
            )

            # Add store tokens to results
            for r in step_results:
                r["store_tokens"] = store_usage.retrieval_tokens

            # Log
            correct = sum(1 for r in step_results if r["task_success"])
            mean_tcca = sum(r["tcca"] for r in step_results) / len(step_results) if step_results else 0
            mean_total = sum(r["total_tokens"] for r in step_results) / len(step_results) if step_results else 0

            for r in step_results:
                out.write(json.dumps(r) + "\n")
            out.flush()

            print(f"    Step {step}/{n_steps}: {correct}/{len(questions)} correct | mean TCCA={mean_tcca:.2f} | mean tokens={mean_total:,.0f} | store={store_usage.retrieval_tokens:,}")


def main():
    parser = argparse.ArgumentParser(description="TCCA Benchmark on BEAM")
    parser.add_argument("--trials", required=True, help="Trials directory")
    parser.add_argument("--adapters", default="accumulate,bm25,vector-rag,llm-compression")
    parser.add_argument("--model", default="gemma4:26b")
    parser.add_argument("--eval-model", default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--output", default="results/beam_run")
    parser.add_argument("--questions-per-step", type=int, default=0,
                        help="Limit questions per step (0=all 20). Use 5 for faster smoke tests.")
    args = parser.parse_args()

    eval_model = args.eval_model or args.model
    trials_dir = Path(args.trials)
    output_dir = Path(args.output)
    adapter_names = args.adapters.split(",")

    manifest = json.loads((trials_dir / "manifest.json").read_text())
    if args.limit > 0:
        manifest = manifest[:args.limit]

    print(f"\n{'#'*60}")
    print(f"# TCCA Benchmark on BEAM")
    print(f"# Trials:   {len(manifest)}")
    print(f"# Adapters: {adapter_names}")
    print(f"# Model:    {args.model}")
    print(f"# Output:   {output_dir}")
    print(f"{'#'*60}")

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "config.json").write_text(json.dumps({
        "adapters": adapter_names,
        "model": args.model,
        "eval_model": eval_model,
        "dataset": "BEAM",
        "n_trials": len(manifest),
    }, indent=2))

    # Baseline cache
    baseline_cache_path = output_dir / "baseline_cache.json"
    baseline_cache = {}
    if baseline_cache_path.exists():
        baseline_cache = json.loads(baseline_cache_path.read_text())

    for i, trial_meta in enumerate(manifest):
        conv_id = trial_meta["conversation_id"]
        trial_dir = trials_dir / conv_id
        meta = json.loads((trial_dir / "meta.json").read_text())

        # Limit questions if requested
        if args.questions_per_step > 0:
            meta["questions"] = meta["questions"][:args.questions_per_step]

        print(f"\n{'='*60}")
        print(f"Trial {i+1}/{len(manifest)}: conv {conv_id} | {meta['n_steps']} steps | {len(meta['questions'])} questions | ~{trial_meta['total_tokens']:,} tokens")
        print(f"{'='*60}")

        # Baseline
        sample_key = f"{conv_id}_1_{meta['questions'][0]['question_id']}"
        if sample_key not in baseline_cache:
            print("\n  --- Accumulate baseline ---")
            trial_baseline = run_baseline_trial(trial_dir, meta, args.model)
            baseline_cache.update(trial_baseline)
            baseline_cache_path.write_text(json.dumps(baseline_cache))
        else:
            print(f"  Baseline cached for conv {conv_id}")

        # Run each adapter
        for adapter_name in adapter_names:
            trial_output = output_dir / adapter_name / conv_id
            run_trial(trial_dir, meta, adapter_name, args.model, eval_model, trial_output, baseline_cache)

    # Summary
    print(f"\n{'='*60}")
    print("Summary")
    print(f"{'='*60}")

    for adapter_name in adapter_names:
        records = []
        adapter_dir = output_dir / adapter_name
        if adapter_dir.exists():
            for td in sorted(adapter_dir.iterdir()):
                rf = td / "results.jsonl"
                if rf.exists():
                    with open(rf) as f:
                        for line in f:
                            if line.strip():
                                records.append(json.loads(line))

        if not records:
            continue

        correct = sum(1 for r in records if r["task_success"])
        mean_tcca = sum(r["tcca"] for r in records) / len(records)
        mean_tokens = sum(r["total_tokens"] for r in records) / len(records)

        # By question type
        by_type = {}
        for r in records:
            qt = r["question_type"]
            if qt not in by_type:
                by_type[qt] = {"correct": 0, "total": 0, "tcca_sum": 0}
            by_type[qt]["total"] += 1
            by_type[qt]["tcca_sum"] += r["tcca"]
            if r["task_success"]:
                by_type[qt]["correct"] += 1

        print(f"\n  {adapter_name}: {correct}/{len(records)} correct ({correct/len(records)*100:.1f}%) | mean TCCA={mean_tcca:.2f} | mean tokens={mean_tokens:,.0f}")
        print(f"  {'Type':<30} {'Acc':>6} {'TCCA':>8}")
        print(f"  {'-'*44}")
        for qt in sorted(by_type.keys()):
            s = by_type[qt]
            acc = s["correct"] / s["total"] if s["total"] else 0
            tcca = s["tcca_sum"] / s["total"] if s["total"] else 0
            print(f"  {qt:<30} {acc:>5.0%} {tcca:>8.2f}")

    (output_dir / "summary.json").write_text(json.dumps({"status": "complete"}, indent=2))
    print(f"\nResults in: {output_dir}")


if __name__ == "__main__":
    main()
