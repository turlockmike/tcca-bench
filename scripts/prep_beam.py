#!/usr/bin/env python3
"""Prepare BEAM dataset for TCCA benchmark.

Slices BEAM conversations into chronological steps and extracts
probing questions with ground-truth answers.

Usage:
    python scripts/prep_beam.py --split 1M --step-size 1 --output data/trials --limit 5
"""

import argparse
import ast
import json
from pathlib import Path

from datasets import load_dataset


def session_to_markdown(session: list[dict], session_index: int) -> str:
    """Convert a BEAM session (list of turns) to markdown."""
    lines = [f"# Session {session_index + 1}"]
    lines.append("")
    for turn in session:
        role = turn.get("role", "unknown").capitalize()
        content = turn.get("content", "").strip()
        time_anchor = turn.get("time_anchor", "")
        if time_anchor:
            lines.append(f"**{role}** [{time_anchor}]: {content}")
        else:
            lines.append(f"**{role}:** {content}")
        lines.append("")
    return "\n".join(lines)


def extract_questions(row: dict) -> list[dict]:
    """Extract all probing questions from a BEAM row."""
    raw = row.get("probing_questions", "")
    if isinstance(raw, str):
        try:
            pq = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            try:
                pq = json.loads(raw)
            except json.JSONDecodeError:
                return []
    else:
        pq = raw

    questions = []
    for qtype, qs in pq.items():
        for i, q in enumerate(qs):
            # Normalize answer field (different types use different keys)
            answer = (
                q.get("answer")
                or q.get("ideal_answer")
                or q.get("ideal_response")
                or q.get("expected_answer")
                or ""
            )
            questions.append({
                "question_id": f"{qtype}_{i}",
                "question_type": qtype,
                "question": q.get("question", ""),
                "answer": answer,
                "difficulty": q.get("difficulty", ""),
            })
    return questions


def prep_trial(row: dict, output_dir: Path, step_size: int):
    """Prepare one trial (one BEAM conversation)."""
    conv_id = str(row["conversation_id"])
    trial_dir = output_dir / conv_id
    steps_dir = trial_dir / "steps"

    chat = row["chat"]  # list of sessions, each a list of turns
    n_sessions = len(chat)
    n_steps = (n_sessions + step_size - 1) // step_size

    # Write sessions as markdown into step directories
    for step in range(1, n_steps + 1):
        step_dir = steps_dir / f"step_{step:02d}"
        step_dir.mkdir(parents=True, exist_ok=True)

        start = (step - 1) * step_size
        end = min(step * step_size, n_sessions)

        for i in range(start, end):
            md = session_to_markdown(chat[i], i)
            (step_dir / f"session_{i+1:03d}.md").write_text(md)

    # Extract questions
    questions = extract_questions(row)

    # Compute approximate token counts per step
    cumulative_chars = 0
    step_token_counts = []
    for step in range(1, n_steps + 1):
        start = (step - 1) * step_size
        end = min(step * step_size, n_sessions)
        for i in range(start, end):
            for turn in chat[i]:
                cumulative_chars += len(turn.get("content", ""))
        step_token_counts.append(cumulative_chars // 4)

    # Write meta
    meta = {
        "conversation_id": conv_id,
        "n_sessions": n_sessions,
        "n_steps": n_steps,
        "step_size": step_size,
        "n_questions": len(questions),
        "question_types": list(set(q["question_type"] for q in questions)),
        "questions": questions,
        "step_cumulative_tokens": step_token_counts,
        "user_profile": row.get("user_profile", {}),
    }
    (trial_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    return meta


def main():
    parser = argparse.ArgumentParser(description="Prep BEAM data for TCCA benchmark")
    parser.add_argument("--split", default="1M", choices=["100K", "500K", "1M"],
                        help="BEAM dataset split (default: 1M)")
    parser.add_argument("--step-size", type=int, default=1,
                        help="Sessions per step (default: 1 — each session is a step)")
    parser.add_argument("--output", default="data/trials", help="Output directory")
    parser.add_argument("--limit", type=int, default=0, help="Max conversations (0=all)")
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading BEAM {args.split} split...")
    ds = load_dataset("Mohammadta/BEAM", split=args.split)

    entries = list(ds)
    if args.limit > 0:
        entries = entries[:args.limit]

    print(f"Preparing {len(entries)} trials (step_size={args.step_size})")

    manifest = []
    total_questions = 0
    for i, row in enumerate(entries):
        meta = prep_trial(row, output_dir, args.step_size)
        manifest.append({
            "conversation_id": meta["conversation_id"],
            "n_steps": meta["n_steps"],
            "n_questions": meta["n_questions"],
            "question_types": meta["question_types"],
            "total_tokens": meta["step_cumulative_tokens"][-1] if meta["step_cumulative_tokens"] else 0,
        })
        total_questions += meta["n_questions"]
        if (i + 1) % 5 == 0 or i == len(entries) - 1:
            print(f"  {i+1}/{len(entries)} conversations | {total_questions} questions")

    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    print(f"\nDone. {len(manifest)} trials, {total_questions} questions in {output_dir}/")
    print(f"Manifest: {output_dir}/manifest.json")

    # Summary
    tokens = [m["total_tokens"] for m in manifest]
    if tokens:
        print(f"Tokens per conversation: min={min(tokens):,} max={max(tokens):,} mean={sum(tokens)//len(tokens):,}")


if __name__ == "__main__":
    main()
