#!/usr/bin/env python3
"""Aggregate longitudinal CQ results and compute kappa (CQ slope) per condition.

Usage:
    python cq-report.py --results results/run_001 --conditions control,bm25,compression
"""

import argparse
import json
import statistics
from pathlib import Path


def load_results(results_dir: Path, conditions: list[str]) -> dict:
    """Load all results.jsonl files, grouped by condition."""
    data = {}
    for condition in conditions:
        cond_dir = results_dir / condition
        if not cond_dir.exists():
            continue
        records = []
        for trial_dir in sorted(cond_dir.iterdir()):
            results_file = trial_dir / "results.jsonl"
            if results_file.exists():
                with open(results_file) as f:
                    for line in f:
                        if line.strip():
                            records.append(json.loads(line))
        data[condition] = records
    return data


def compute_kappa(steps: list[int], cq_values: list[float]) -> float:
    """Simple linear regression slope of CQ over steps.

    kappa > 0: CQ improving (compression learning)
    kappa ≈ 0: CQ holding (compression adequate)
    kappa < 0: CQ degrading (compression failing)
    """
    n = len(steps)
    if n < 2:
        return 0.0
    mean_x = sum(steps) / n
    mean_y = sum(cq_values) / n
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(steps, cq_values))
    denominator = sum((x - mean_x) ** 2 for x in steps)
    if denominator == 0:
        return 0.0
    return numerator / denominator


def analyze_condition(records: list[dict]) -> dict:
    """Analyze one condition's results."""
    if not records:
        return {}

    # Separate pre-answer and post-answer
    post_answer = [r for r in records if r.get("is_post_answer", False)]
    all_records = records

    # Per-step aggregation (post-answer only for CQ slope)
    by_step = {}
    for r in post_answer:
        step = r["step"]
        if step not in by_step:
            by_step[step] = []
        by_step[step].append(r)

    step_stats = {}
    for step in sorted(by_step.keys()):
        rs = by_step[step]
        successes = sum(1 for r in rs if r["task_success"])
        cqs = [r["cq"] for r in rs]
        step_stats[step] = {
            "n_trials": len(rs),
            "task_success_rate": round(successes / len(rs), 4) if rs else 0,
            "mean_cq": round(statistics.mean(cqs), 4) if cqs else 0,
            "mean_N": round(statistics.mean([r["N"] for r in rs])),
            "mean_K": round(statistics.mean([r["K"] for r in rs])),
            "mean_compression_ratio": round(statistics.mean([r["compression_ratio"] for r in rs]), 4),
        }

    # Compute kappa (slope of mean CQ over steps)
    if len(step_stats) >= 2:
        steps = sorted(step_stats.keys())
        mean_cqs = [step_stats[s]["mean_cq"] for s in steps]
        kappa = compute_kappa(steps, mean_cqs)
    else:
        kappa = 0.0

    # Overall stats (post-answer)
    all_cqs = [r["cq"] for r in post_answer]
    all_success = sum(1 for r in post_answer if r["task_success"])

    return {
        "n_total_records": len(all_records),
        "n_post_answer_records": len(post_answer),
        "task_success_rate": round(all_success / len(post_answer), 4) if post_answer else 0,
        "mean_cq": round(statistics.mean(all_cqs), 4) if all_cqs else 0,
        "median_cq": round(statistics.median(all_cqs), 4) if all_cqs else 0,
        "kappa": round(kappa, 6),
        "by_step": step_stats,
    }


def main():
    parser = argparse.ArgumentParser(description="CQ Longitudinal Report")
    parser.add_argument("--results", required=True, help="Results directory")
    parser.add_argument("--conditions", default="control,bm25,compression")
    args = parser.parse_args()

    results_dir = Path(args.results)
    conditions = args.conditions.split(",")

    data = load_results(results_dir, conditions)

    summary = {}
    for condition in conditions:
        records = data.get(condition, [])
        if not records:
            print(f"  {condition}: no data")
            continue
        summary[condition] = analyze_condition(records)

    # Print comparison
    print(f"\n{'='*70}")
    print("Longitudinal CQ Report")
    print(f"{'='*70}")
    print(f"  {'Condition':<20} {'Success':>8} {'Mean CQ':>10} {'κ (slope)':>12} {'Records':>8}")
    print(f"  {'-'*58}")

    for condition in conditions:
        s = summary.get(condition, {})
        if not s:
            continue
        kappa = s["kappa"]
        kappa_indicator = "↑" if kappa > 0.1 else ("↓" if kappa < -0.1 else "→")
        print(f"  {condition:<20} {s['task_success_rate']:>7.1%} {s['mean_cq']:>10.4f} {kappa:>10.4f} {kappa_indicator} {s['n_post_answer_records']:>7}")

    # Per-step detail for each condition
    for condition in conditions:
        s = summary.get(condition, {})
        if not s or not s.get("by_step"):
            continue
        print(f"\n  --- {condition}: CQ by step (post-answer only) ---")
        print(f"  {'Step':>6} {'Success':>8} {'Mean CQ':>10} {'Mean K':>10} {'Mean N':>10} {'Ratio':>8}")
        for step in sorted(s["by_step"].keys()):
            st = s["by_step"][step]
            print(f"  {step:>6} {st['task_success_rate']:>7.1%} {st['mean_cq']:>10.4f} {st['mean_K']:>10,} {st['mean_N']:>10,} {st['mean_compression_ratio']:>8.4f}")

    # Write summary JSON
    summary_path = results_dir / "summary.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nSummary: {summary_path}")


if __name__ == "__main__":
    main()
