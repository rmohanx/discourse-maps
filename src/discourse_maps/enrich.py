"""Enrich Sources from scholarly-metadata APIs (OpenAlex first, Crossref for missing abstracts).

For each Document in a space:
  1. Find the work in OpenAlex: by DOI when we have one, otherwise by title search filtered
     to the publication year, accepted only on a close title match plus first-author match.
  2. Store the OpenAlex id (and DOI if newly found) as external identifiers.
  3. Append the abstract to the Content text; mark its embedding obsolete.
  4. Keep citation counts, topics, and referenced works in Document.metadata.openalex.
Then link Sources that cite each other inside the space as "cites" relation Concepts.
"""

import re
import time
from difflib import SequenceMatcher

import httpx
import psycopg
from psycopg.types.json import Jsonb

from discourse_maps import db
from discourse_maps.config import get_settings
from discourse_maps.importer import ZITTER_SPACE, ensure_schemas

OPENALEX = "https://api.openalex.org"
CROSSREF = "https://api.crossref.org"
TITLE_MATCH = 0.88  # title similarity needed to accept a search hit...
TITLE_MATCH_SAME_YEAR = 0.75  # ...or this, when the year is exact (first author must match either way)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower())


def title_similarity(a: str, b: str) -> float:
    """Best of character similarity as written and with words sorted (order-insensitive)."""
    a, b = _norm(a), _norm(b)
    as_written = SequenceMatcher(None, a, b).ratio()
    word_order_free = SequenceMatcher(None, " ".join(sorted(a.split())), " ".join(sorted(b.split()))).ratio()
    return max(as_written, word_order_free)


def abstract_from_inverted_index(index: dict[str, list[int]] | None) -> str | None:
    """OpenAlex ships abstracts as {word: [positions]}; rebuild the text."""
    if not index:
        return None
    words = {pos: word for word, positions in index.items() for pos in positions}
    return " ".join(words[i] for i in sorted(words))


def strip_jats(text: str | None) -> str | None:
    """Crossref abstracts are JATS XML fragments."""
    if not text:
        return None
    text = re.sub(r"<jats:title>.*?</jats:title>", "", text, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip() or None


def short_openalex_id(url: str) -> str:
    return url.rsplit("/", 1)[-1]


class RateLimited(Exception):
    """The API asked us to wait longer than is worth blocking for."""


class ScholarlyClient:
    MAX_WAIT = 60  # seconds; longer Retry-After values mean "budget exhausted, come back later"

    def __init__(self, mailto: str | None = None, openalex_key: str | None = None, delay: float = 0.15):
        self.params = {"mailto": mailto} if mailto else {}
        self.openalex_headers = {"Authorization": f"Bearer {openalex_key}"} if openalex_key else {}
        self.delay = delay
        self.http = httpx.Client(timeout=30, follow_redirects=True)

    def _get(self, url: str, **params) -> dict | None:
        headers = self.openalex_headers if url.startswith(OPENALEX) else {}
        for attempt in range(4):
            time.sleep(self.delay)
            resp = self.http.get(url, params={**self.params, **params}, headers=headers)
            if resp.status_code == 404:
                return None
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = int(resp.headers.get("retry-after", 2**attempt))
                if wait > self.MAX_WAIT:
                    hint = "" if headers or not url.startswith(OPENALEX) else " Set OPENALEX_API_KEY (free)."
                    raise RateLimited(f"{url.split('?')[0]}: retry after {wait}s.{hint}")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RateLimited(f"{url.split('?')[0]}: still failing after retries")

    def work_by_doi(self, doi: str) -> dict | None:
        return self._get(f"{OPENALEX}/works/doi:{doi}")

    def search_work(self, title: str, year: int | None, surname: str) -> tuple[dict | None, float]:
        # OpenAlex search chokes on some punctuation; keep words only.
        query = " ".join(_norm(title).split()[:20])
        params = {"search": query, "per-page": "5"}
        if year:
            params["filter"] = f"publication_year:{year - 1}-{year + 1}"
        data = self._get(f"{OPENALEX}/works", **params) or {}
        best, best_score = None, 0.0
        for work in data.get("results", []):
            names = " ".join(
                (a.get("author") or {}).get("display_name") or "" for a in work.get("authorships", [])
            )
            if surname and _norm(surname) not in _norm(names):
                continue
            score = title_similarity(title, work.get("title") or "")
            threshold = TITLE_MATCH_SAME_YEAR if work.get("publication_year") == year else TITLE_MATCH
            if score >= threshold and score > best_score:
                best, best_score = work, score
        return best, round(best_score, 3)

    def crossref_abstract(self, doi: str) -> str | None:
        data = self._get(f"{CROSSREF}/works/{doi}")
        return strip_jats((data or {}).get("message", {}).get("abstract"))


def _documents(conn: psycopg.Connection, space_id: int, force: bool) -> list[dict]:
    return conn.execute(
        """
        select d.id, d.source_local_id, d.metadata, c.id as content_id,
               (select value from external_identifier e where e.entity_id = d.id and e.scheme = 'doi') as doi
        from "Document" d
        join "Content" c on c.document_id = d.id and c.variant = 'direct'
        where d.space_id = %s and (%s or not d.metadata ? 'openalex')
        order by d.id
        """,
        (space_id, force),
    ).fetchall()


def enrich_space(
    conn: psycopg.Connection,
    limit: int | None = None,
    force: bool = False,
    client: ScholarlyClient | None = None,
) -> dict[str, int]:
    settings = get_settings()
    client = client or ScholarlyClient(settings.scholarly_mailto, settings.openalex_api_key)
    space_id = db.ensure_space(conn, *ZITTER_SPACE, platform="Bibtex")
    agent_id = db.ensure_agent(conn)
    docs = _documents(conn, space_id, force)[:limit]
    with db.activity(conn, "enrich:openalex", agent_id, {"limit": limit, "force": force}) as act:
        try:
            _enrich_documents(conn, client, docs, act)
        except RateLimited as exc:
            conn.rollback()  # drop the half-done document; finished ones are already committed
            act.summary["stopped"] = str(exc)
        act.count("citations", link_citations(conn, space_id, agent_id, act))
    return act.summary


def _enrich_documents(
    conn: psycopg.Connection, client: ScholarlyClient, docs: list[dict], act: db.Activity
) -> None:
    for doc in docs:
        meta = doc["metadata"]
        surname = (meta.get("authors") or [""])[0].split(",")[0]
        work = client.work_by_doi(doc["doi"]) if doc["doi"] else None
        how, score = ("doi", 1.0) if work else (None, None)
        if not work:
            work, score = client.search_work(meta.get("title", ""), meta.get("year"), surname)
            how = "search" if work else None
        if not work:
            # Remember the miss so later runs skip it unless --force.
            conn.execute(
                """update "Document" set metadata = metadata || '{"openalex": null}' where id = %s""",
                (doc["id"],),
            )
            act.count("not_found")
            conn.commit()
            continue

        doi = (work.get("doi") or "").removeprefix("https://doi.org/").lower() or doc["doi"]
        abstract = abstract_from_inverted_index(work.get("abstract_inverted_index"))
        if not abstract and doi:
            abstract = client.crossref_abstract(doi)
            if abstract:
                act.count("abstract_from_crossref")

        openalex = {
            "id": short_openalex_id(work["id"]),
            "matched_by": how,
            "match_score": score,
            "title": work.get("title"),
            "cited_by_count": work.get("cited_by_count"),
            "publication_year": work.get("publication_year"),
            "type": work.get("type"),
            "is_oa": (work.get("open_access") or {}).get("is_oa"),
            "oa_url": (work.get("open_access") or {}).get("oa_url"),
            "topics": [t.get("display_name") for t in work.get("topics", [])[:5]],
            "referenced_works": [short_openalex_id(w) for w in work.get("referenced_works", [])],
            "has_abstract": bool(abstract),
        }
        conn.execute(
            """update "Document" set metadata = metadata || %s, last_modified = now() where id = %s""",
            (Jsonb({"openalex": openalex}), doc["id"]),
        )
        db_ids = [("openalex", openalex["id"]), ("doi", doi)]
        for scheme, value in db_ids:
            if value:
                conn.execute(
                    """
                    insert into external_identifier (entity_id, scheme, value) values (%s, %s, %s)
                    on conflict do nothing
                    """,
                    (doc["id"], scheme, value),
                )
        act.revised(doc["id"])
        act.count(f"matched_by_{how}")

        if abstract:
            title = meta.get("title", "")
            conn.execute(
                """update "Content" set text = %s, last_modified = now() where id = %s""",
                (f"{title}\n\n{abstract}", doc["content_id"]),
            )
            conn.execute(
                """
                update "ContentEmbedding_qwen3_embedding_0_6b_1024" set obsolete = true
                where target_id = %s
                """,
                (doc["content_id"],),
            )
            act.revised(doc["content_id"])
            act.count("abstracts")
        conn.commit()


def link_citations(conn: psycopg.Connection, space_id: int, agent_id: int, act: db.Activity) -> int:
    """Create a 'cites' relation Concept for every citation between two Sources in the space."""
    schemas = ensure_schemas(conn, space_id, agent_id)
    pairs = conn.execute(
        """
        with src as (
            select d.source_local_id as key, ref.value as cited_openalex
            from "Document" d, jsonb_array_elements_text(d.metadata->'openalex'->'referenced_works') ref
            where d.space_id = %(space)s
        )
        select s.key as citing, d.source_local_id as cited,
               cs.id as citing_concept, cd.id as cited_concept
        from src s
        join external_identifier e on e.scheme = 'openalex' and e.value = s.cited_openalex
        join "Document" d on d.id = e.entity_id and d.space_id = %(space)s
        join "Concept" cs on cs.space_id = %(space)s and cs.source_local_id = 'source:' || s.key
        join "Concept" cd on cd.space_id = %(space)s and cd.source_local_id = 'source:' || d.source_local_id
        """,
        {"space": space_id},
    ).fetchall()
    for p in pairs:
        row = conn.execute(
            """
            insert into "Concept" (name, space_id, author_id, schema_id, is_relation, arity,
                                   reference_content, source_local_id, epistemic_status)
            values ('cites', %s, %s, %s, true, 2, %s, %s, 'certain')
            on conflict (space_id, source_local_id) do nothing
            returning id
            """,
            (
                space_id,
                agent_id,
                schemas["cites"],
                Jsonb({"source": p["citing_concept"], "destination": p["cited_concept"]}),
                f"cites:{p['citing']}->{p['cited']}",
            ),
        ).fetchone()
        if row:
            act.record(row["id"], inserted=True)
    conn.commit()
    return len(pairs)
