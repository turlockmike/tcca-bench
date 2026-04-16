#!/usr/bin/env python3
"""Slice LongMemEval sessions into step directories for longitudinal CQ benchmark.

Each question becomes a trial. Its ~40-48 sessions are divided into steps of
step_size sessions. Each session is written as a markdown file.

Usage:
    python prep_data.py --dataset ../data/longmemeval_s_cleaned.json --step-size 6 --limit 10
"""

import argparse
import json
import os
from pathlib import Path


def session_to_markdown(session: list[dict], session_id: str, date: str, index: int) -> str:
    """Convert a session (list of turns) to markdown."""
    lines = [f"# Session {index:03d} | {date}", ""]
    for turn in session:
        role = turn["role"].capitalize()
        content = turn["content"].strip()
        lines.append(f"**{role}:** {content}")
        lines.append("")
    return "\n".join(lines)


def find_answer_step(session_ids: list[str], answer_session_ids: list[str], step_size: int) -> int:
    """Find which step first contains an answer session. Returns 1-indexed step number."""
    answer_set = set(answer_session_ids)
    for i, sid in enumerate(session_ids):
        if sid in answer_set:
            return (i // step_size) + 1
    return -1  # answer not found (shouldn't happen for well-formed data)


def prep_trial(entry: dict, output_dir: Path, step_size: int):
    """Prepare one trial directory from a LongMemEval question."""
    qid = entry["question_id"]
    trial_dir = output_dir / qid
    steps_dir = trial_dir / "steps"

    sessions = entry["haystack_sessions"]
    dates = entry.get("haystack_dates", [""] * len(sessions))
    session_ids = entry.get("haystack_session_ids", [f"s{i}" for i in range(len(sessions))])
    answer_session_ids = entry.get("answer_session_ids", [])

    n_sessions = len(sessions)
    n_steps = (n_sessions + step_size - 1) // step_size  # ceiling division
    answer_step = find_answer_step(session_ids, answer_session_ids, step_size)

    # Write sessions as markdown files into step directories
    sessions_per_step = []
    for step in range(1, n_steps + 1):
        step_dir = steps_dir / f"step_{step:02d}"
        step_dir.mkdir(parents=True, exist_ok=True)

        start = (step - 1) * step_size
        end = min(step * step_size, n_sessions)
        step_count = 0

        for i in range(start, end):
            date = dates[i] if i < len(dates) else ""
            sid = session_ids[i] if i < len(session_ids) else f"s{i}"
            md = session_to_markdown(sessions[i], sid, date, i + 1)
            filename = f"{i+1:03d}_{sid[:20]}.md"
            (step_dir / filename).write_text(md)
            step_count += 1

        sessions_per_step.append(step_count)

    # Write meta.json
    meta = {
        "question_id": qid,
        "question": entry["question"],
        "answer": entry["answer"],
        "question_type": entry.get("question_type", ""),
        "question_date": entry.get("question_date", ""),
        "n_sessions": n_sessions,
        "n_steps": n_steps,
        "step_size": step_size,
        "answer_step": answer_step,
        "answer_session_ids": answer_session_ids,
        "sessions_per_step": sessions_per_step,
    }
    (trial_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    return meta


def main():
    parser = argparse.ArgumentParser(description="Prep LongMemEval data for longitudinal CQ benchmark")
    parser.add_argument("--dataset", required=True, help="Path to longmemeval_s_cleaned.json")
    parser.add_argument("--step-size", type=int, default=6, help="Sessions per step (default: 6)")
    parser.add_argument("--output", default="data/trials", help="Output directory")
    parser.add_argument("--limit", type=int, default=0, help="Max questions to prep (0=all)")
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load dataset
    with open(dataset_path) as f:
        first = f.read(1)
        f.seek(0)
        entries = json.load(f) if first == "[" else [json.loads(l) for l in f if l.strip()]

    if args.limit > 0:
        entries = entries[:args.limit]

    print(f"Preparing {len(entries)} trials (step_size={args.step_size})")

    manifest = []
    for i, entry in enumerate(entries):
        meta = prep_trial(entry, output_dir, args.step_size)
        manifest.append({
            "question_id": meta["question_id"],
            "question_type": meta["question_type"],
            "n_steps": meta["n_steps"],
            "answer_step": meta["answer_step"],
        })
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(entries)}")

    # Write manifest
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\nDone. {len(manifest)} trials in {output_dir}/")
    print(f"Manifest: {output_dir}/manifest.json")

    # Summary stats
    answer_steps = [m["answer_step"] for m in manifest if m["answer_step"] > 0]
    if answer_steps:
        print(f"Answer step: min={min(answer_steps)}, max={max(answer_steps)}, mean={sum(answer_steps)/len(answer_steps):.1f}")


if __name__ == "__main__":
    main()
