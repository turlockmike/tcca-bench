#!/usr/bin/env python3
"""BM25 retrieval over a file-based knowledge base.

Reads all .md files from a directory, runs BM25 against a query,
outputs the top-K file contents concatenated to stdout.

Usage:
    python retrieve.py --kb /path/to/kb --query "What degree?" --topk 5
"""

import argparse
from pathlib import Path

from rank_bm25 import BM25Okapi


def retrieve(kb_dir: str, query: str, topk: int = 5) -> str:
    """BM25 retrieval over markdown files. Returns concatenated top-K content."""
    kb_path = Path(kb_dir)
    files = sorted(kb_path.glob("**/*.md"))

    if not files:
        return ""

    # Build corpus
    corpus_texts = []
    corpus_paths = []
    for f in files:
        text = f.read_text()
        corpus_texts.append(text)
        corpus_paths.append(f)

    # BM25
    tokenized = [t.split() for t in corpus_texts]
    bm25 = BM25Okapi(tokenized)
    scores = bm25.get_scores(query.split())

    # Rank and select top-K
    ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    selected = ranked[:topk]

    # Concatenate in original file order (chronological)
    selected.sort()
    parts = []
    for i in selected:
        parts.append(f"--- {corpus_paths[i].name} ---\n{corpus_texts[i]}")

    return "\n\n".join(parts)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kb", required=True, help="KB directory path")
    parser.add_argument("--query", required=True, help="Search query")
    parser.add_argument("--topk", type=int, default=5, help="Number of files to retrieve")
    args = parser.parse_args()

    result = retrieve(args.kb, args.query, args.topk)
    print(result)


if __name__ == "__main__":
    main()
