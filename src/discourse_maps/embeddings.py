"""Embedding providers and the job that keeps Content embeddings current.

Following upstream's design, each model has its own table (named after model and
dimensions), so models can be added or swapped without mixing incomparable vectors.
"""

from typing import Protocol

import psycopg

from discourse_maps import db


class EmbeddingProvider(Protocol):
    name: str  # matches the EmbeddingName enum value
    dimensions: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class Qwen3Provider:
    """Qwen3-Embedding-0.6B, run locally with sentence-transformers (Apple GPU via MPS)."""

    name = "qwen3_embedding_0_6b_1024"
    dimensions = 1024
    model_id = "Qwen/Qwen3-Embedding-0.6B"

    def __init__(self):
        import torch
        from sentence_transformers import SentenceTransformer

        device = "mps" if torch.backends.mps.is_available() else "cpu"
        self.model = SentenceTransformer(self.model_id, device=device)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.model.encode(texts, batch_size=16, normalize_embeddings=True).tolist()

    def embed_query(self, text: str) -> list[float]:
        # Qwen3 embeddings expect an instruction prompt on the query side only.
        return self.model.encode([text], prompt_name="query", normalize_embeddings=True)[0].tolist()


PROVIDERS = {"qwen3": Qwen3Provider}


def get_provider(name: str) -> EmbeddingProvider:
    try:
        return PROVIDERS[name]()
    except KeyError:
        raise ValueError(f"Unknown embedding provider {name!r}; known: {sorted(PROVIDERS)}") from None


def table_for(provider: EmbeddingProvider) -> str:
    return f'"ContentEmbedding_{provider.name}"'


def to_pgvector(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"


def embed_pending(conn: psycopg.Connection, provider: EmbeddingProvider, batch: int = 32) -> dict[str, int]:
    """Embed Content rows that have no embedding yet, or whose embedding is obsolete."""
    table = table_for(provider)
    rows = conn.execute(
        f"""
        select c.id, c.text from "Content" c
        left join {table} e on e.target_id = c.id
        where e.target_id is null or e.obsolete
        order by c.id
        """
    ).fetchall()
    agent_id = db.ensure_agent(conn)
    with db.activity(conn, f"embed:{provider.name}", agent_id, {"model": provider.name}) as act:
        for start in range(0, len(rows), batch):
            chunk = rows[start : start + batch]
            vectors = provider.embed_documents([r["text"] for r in chunk])
            for row, vec in zip(chunk, vectors, strict=True):
                conn.execute(
                    f"""
                    insert into {table} (target_id, vector, obsolete) values (%s, %s::extensions.vector, false)
                    on conflict (target_id) do update set vector = excluded.vector, obsolete = false
                    """,
                    (row["id"], to_pgvector(vec)),
                )
            conn.commit()
            act.count("embedded", len(chunk))
    return act.summary
