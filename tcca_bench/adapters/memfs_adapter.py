"""memfs adapter: Neo4j-backed graph memory, zero-LLM retrieval.

Third-party adapter demonstrating the interface works for memory systems that
aren't built for this benchmark. memfs (https://github.com/turlockmike/memfs) is
a file-backed, Neo4j-indexed memory store with Lucene fulltext + link/search
edges over nodes.

Design choices for TCCA instrumentation:

- **store()** — writes the session as a Neo4j :Node (path, title, content).
  Zero LLM tokens: the indexer is programmatic (hash + fulltext insert).
- **query()** — BM25 fulltext over :Node.content, top-K by score, optional
  one-hop :LINK expansion. The top-K contents are joined and passed to the
  answer LLM. retrieval_tokens=0 (all retrieval is Cypher/Lucene).
- **Trial isolation** — every adapter instance owns a `trial_id`-scoped path
  prefix (`tcca/<trial_id>/`). Fulltext queries filter by prefix; `reset()`
  deletes only nodes under that prefix. Safe to point at a shared Neo4j.
- **Schema** — uses the standard memfs schema (fulltext index `node_content`
  on title+description+content). `create_db(fresh=False)` is idempotent.

Requires:
- memfs installed and importable (`pip install git+https://github.com/turlockmike/memfs`)
- Neo4j reachable at MEMFS_NEO4J_URI (default bolt://localhost:7687)
- Ollama (same as the other adapters) for the answer LLM
"""

from __future__ import annotations

import hashlib
import uuid

import httpx

from tcca_bench.adapter import MemoryAdapter, TokenUsage

OLLAMA_URL = "http://localhost:11434"


class MemfsAdapter(MemoryAdapter):
    """memfs-backed memory adapter (Neo4j graph store + Lucene fulltext)."""

    name = "memfs"

    def __init__(
        self,
        model: str = "gemma4:26b",
        ollama_url: str = OLLAMA_URL,
        topk: int = 5,
        expand_hops: int = 0,
        trial_id: str | None = None,
        neo4j_uri: str | None = None,
    ):
        """Construct a memfs adapter.

        Parameters
        ----------
        model : str
            Ollama model tag for the answer LLM.
        ollama_url : str
            Ollama server URL.
        topk : int
            Number of top fulltext matches to load into context.
        expand_hops : int
            If >0, include neighbor nodes reached via `expand_hops` :LINK hops.
            0 = pure fulltext (the default). 1 = graph-hypothesis mode.
        trial_id : str | None
            Path-prefix namespace for this adapter's nodes. Auto-generated if
            omitted. Lets multiple trials share one Neo4j safely.
        neo4j_uri : str | None
            Override `MEMFS_NEO4J_URI`. Mostly useful for tests.
        """
        self.model = model
        self.ollama_url = ollama_url
        self.topk = topk
        self.expand_hops = expand_hops
        self.trial_id = trial_id or uuid.uuid4().hex[:12]
        self.path_prefix = f"tcca/{self.trial_id}/"

        # Lazy import so memfs is a soft dependency: the rest of the benchmark
        # keeps working if someone doesn't want a Neo4j instance running.
        try:
            from memfs.graph import connect, create_db
            from memfs.search import _escape_lucene
        except ImportError as e:  # pragma: no cover - environment-specific
            raise ImportError(
                "memfs is not installed. Install with:\n"
                "    pip install git+https://github.com/turlockmike/memfs\n"
                "and start a Neo4j instance (see memfs docker-compose.yml). "
                "Set MEMFS_NEO4J_URI / MEMFS_NEO4J_PASSWORD as needed."
            ) from e

        # Stash the module references so query()/reset() don't re-import.
        self._connect = connect
        self._escape_lucene = _escape_lucene

        create_db(uri=neo4j_uri, fresh=False)  # idempotent schema apply
        self.graph = connect(uri=neo4j_uri)

        # Start clean — if a previous run used the same trial_id, wipe it.
        self.reset()

    # --------------------------------------------------------------- store
    def store(self, session_text: str, session_id: str, date: str) -> TokenUsage:
        """Upsert one session as a :Node. No LLM calls.

        Returns zero TokenUsage: indexing is programmatic (hash + Cypher).
        """
        from memfs.graph import upsert_node

        node_path = f"{self.path_prefix}{session_id}"
        content_hash = hashlib.sha256(session_text.encode("utf-8")).hexdigest()
        # Keep a short description for fulltext boost on the query-side
        description = session_text[:200].replace("\n", " ")

        upsert_node(
            self.graph,
            path=node_path,
            title=session_id,
            content_hash=content_hash,
            date_hint=date or None,
            description=description,
            content=session_text,
            layer=2,
        )
        return TokenUsage()

    # --------------------------------------------------------------- query
    def _fulltext_topk(self, question: str) -> list[dict]:
        """Scoped Lucene fulltext: filter by the trial's path prefix."""
        lucene = self._escape_lucene(question)
        if not lucene:
            return []
        rows = self.graph.run(
            """
            CALL db.index.fulltext.queryNodes('node_content', $q)
                 YIELD node, score
            WHERE node.path STARTS WITH $prefix
            RETURN node.path AS path, node.content AS content, score
            ORDER BY score DESC
            LIMIT $k
            """,
            q=lucene,
            prefix=self.path_prefix,
            k=self.topk,
        )
        return rows

    def _expand_one_hop(self, seed_paths: list[str]) -> list[dict]:
        """Fetch one-hop neighbors via :LINK edges, scoped to this trial.

        Returns additional rows (path, content) not already in seed_paths.
        Edges are created at write time by the memfs indexer when markdown
        references resolve — for raw chat-session text there typically aren't
        any, so this is a no-op unless a caller pre-links sessions.
        """
        if not seed_paths:
            return []
        rows = self.graph.run(
            """
            MATCH (seed:Node)-[:LINK]->(neighbor:Node)
            WHERE seed.path IN $seeds
              AND neighbor.path STARTS WITH $prefix
              AND NOT neighbor.path IN $seeds
            RETURN DISTINCT neighbor.path AS path, neighbor.content AS content
            LIMIT $k
            """,
            seeds=seed_paths,
            prefix=self.path_prefix,
            k=self.topk,
        )
        return rows

    def query(self, question: str) -> tuple[str, TokenUsage]:
        rows = self._fulltext_topk(question)
        if not rows:
            return "I don't know", TokenUsage()

        if self.expand_hops >= 1:
            extras = self._expand_one_hop([r["path"] for r in rows])
            rows = rows + extras

        context = "\n\n".join(
            f"--- {r['path']} ---\n{r.get('content') or ''}" for r in rows
        )

        prompt = (
            "I will give you several history chats between you and a user. "
            "Please answer the question based on the relevant chat history.\n\n"
            f"History Chats:\n{context}\n\n"
            f"Question: {question}\n"
            "Answer concisely:"
        )

        response = httpx.post(
            f"{self.ollama_url}/api/chat",
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "think": False,
                "options": {"temperature": 0, "num_predict": 500},
            },
            timeout=300.0,
        )
        response.raise_for_status()
        data = response.json()

        answer = data.get("message", {}).get("content", "").strip()
        prompt_tokens = data.get("prompt_eval_count", 0)
        completion_tokens = data.get("eval_count", 0)

        usage = TokenUsage(
            retrieval_tokens=0,  # fulltext + optional graph hop = programmatic
            context_tokens=prompt_tokens,
            generation_tokens=completion_tokens,
        )
        return answer, usage

    # --------------------------------------------------------------- reset
    def reset(self):
        """Delete all nodes (and their edges) under this trial's prefix."""
        self.graph.run(
            "MATCH (n:Node) WHERE n.path STARTS WITH $p DETACH DELETE n",
            p=self.path_prefix,
        )

    def close(self):
        """Release the Neo4j driver. Idempotent."""
        if getattr(self, "graph", None) is not None:
            self.graph.close()
            self.graph = None

    def __del__(self):  # pragma: no cover - best-effort cleanup
        try:
            self.close()
        except Exception:
            pass
