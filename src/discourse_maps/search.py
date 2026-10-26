"""Search Content: keyword (full text), fuzzy (trigram), and semantic (vector) with a
serendipity slider.

Serendipity uses Maximal Marginal Relevance over a pool of relevant candidates:
    score = (1 - s) * relevance(query, d) - s * max_similarity(d, already_picked)
s = 0 returns the closest matches; raising s trades closeness for results that differ
from each other, the "stepping stones" idea from Stanley's novelty search, kept anchored
to the query so it stays a search rather than a random walk.
"""

import json
from dataclasses import dataclass

import numpy as np
import psycopg

from discourse_maps.embeddings import EmbeddingProvider, table_for, to_pgvector


@dataclass
class Hit:
    content_id: int
    key: str
    title: str
    year: int | None
    score: float


_SELECT = """
    select c.id, c.source_local_id as key, d.metadata->>'title' as title,
           (d.metadata->>'year')::int as year
"""


def keyword(conn: psycopg.Connection, query: str, k: int = 10) -> list[Hit]:
    rows = conn.execute(
        _SELECT
        + """, ts_rank(c.fts, q) as score
        from "Content" c join "Document" d on d.id = c.document_id,
             websearch_to_tsquery('simple', %s) q
        where c.fts @@ q order by score desc limit %s
        """,
        (query, k),
    ).fetchall()
    return [Hit(r["id"], r["key"], r["title"], r["year"], float(r["score"])) for r in rows]


def fuzzy(conn: psycopg.Connection, query: str, k: int = 10) -> list[Hit]:
    rows = conn.execute(
        _SELECT
        + """, extensions.word_similarity(%s, c.text) as score
        from "Content" c join "Document" d on d.id = c.document_id
        order by score desc limit %s
        """,
        (query, k),
    ).fetchall()
    return [Hit(r["id"], r["key"], r["title"], r["year"], float(r["score"])) for r in rows]


def mmr(query_vec: np.ndarray, cand_vecs: np.ndarray, k: int, serendipity: float) -> list[int]:
    """Indices of cand_vecs chosen by Maximal Marginal Relevance (vectors L2-normalized)."""
    relevance = cand_vecs @ query_vec
    chosen: list[int] = []
    remaining = list(range(len(cand_vecs)))
    while remaining and len(chosen) < k:
        if chosen:
            redundancy = (cand_vecs[remaining] @ cand_vecs[chosen].T).max(axis=1)
        else:
            redundancy = np.zeros(len(remaining))
        scores = (1 - serendipity) * relevance[remaining] - serendipity * redundancy
        chosen.append(remaining.pop(int(np.argmax(scores))))
    return chosen


def semantic(
    conn: psycopg.Connection,
    provider: EmbeddingProvider,
    query: str,
    k: int = 10,
    serendipity: float = 0.0,
    pool: int = 50,
) -> list[Hit]:
    if not 0.0 <= serendipity <= 1.0:
        raise ValueError("serendipity must be between 0 and 1")
    qvec = provider.embed_query(query)
    rows = conn.execute(
        _SELECT
        + f""", e.vector::text as vec, 1 - (e.vector <=> %s::extensions.vector) as score
        from {table_for(provider)} e
        join "Content" c on c.id = e.target_id
        join "Document" d on d.id = c.document_id
        where not e.obsolete
        order by e.vector <=> %s::extensions.vector limit %s
        """,
        (to_pgvector(qvec), to_pgvector(qvec), pool if serendipity else k),
    ).fetchall()
    if not rows:
        return []
    order = range(min(k, len(rows)))
    if serendipity:
        cand = np.array([json.loads(r["vec"]) for r in rows])
        order = mmr(np.array(qvec), cand, k, serendipity)
    return [
        Hit(rows[i]["id"], rows[i]["key"], rows[i]["title"], rows[i]["year"], float(rows[i]["score"]))
        for i in order
    ]
