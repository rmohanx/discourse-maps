# discourse-maps

Python plumbing for **discourse graphs** on **Supabase**, built to work alongside Roam
Research and Obsidian. It's an independent home lab that follows the data model of the
[Discourse Graphs](https://discoursegraphs.com) project
([DiscourseGraphs/discourse-graph](https://github.com/DiscourseGraphs/discourse-graph)) and
adds standards-based layers: provenance, persistent identifiers, role-based access
control, and serendipity-tunable search.

The first dataset is the [Zitter Repository](https://www.zitter-institute.org/p/zitter-repository.html),
177 papers (1915–2026) on the Zitter model of the electron.

## What works today

| Step | Command | What it does |
|---|---|---|
| Fetch | `uv run dm fetch` | Downloads the Zitter BibTeX files and paper list into `data/` (git-ignored) |
| Import | `uv run dm import` | BibTeX → `Document` + `Content` + `Concept` (Source node). Idempotent. |
| Enrich | `uv run dm enrich` | OpenAlex / Crossref: abstracts, DOIs, topics, citation counts, and **`cites` relations** between papers |
| Embed | `uv run dm embed` | Local **Qwen3-Embedding-0.6B** vectors (Apple GPU via MPS); only new or changed rows |
| Search | `uv run dm search "..."` | `--mode keyword \| fuzzy \| semantic`, plus `--serendipity 0..1` |
| Obsidian | `uv run dm export-obsidian` | One note per Source with frontmatter and `[[wiki-links]]` for relations |
| Status | `uv run dm status` | Row counts and the latest pipeline runs (from provenance) |
| PDFs | `uv run dm fetch-pdfs` | Optional, slow, local only. PDFs are never committed (publishers' copyright). |

## Quick start

Requirements: macOS or Linux, [Docker](https://www.docker.com/), [uv](https://docs.astral.sh/uv/),
[Supabase CLI](https://supabase.com/docs/guides/local-development/cli/getting-started).

```bash
git clone https://github.com/rmohanx/discourse-maps && cd discourse-maps
cp .env.example .env              # add a free OPENALEX_API_KEY for enrichment
supabase start                    # local Postgres 17 + API + Studio in Docker (~600 MB RAM)
supabase db reset                 # apply migrations
uv sync
uv run dm fetch && uv run dm import && uv run dm enrich && uv run dm embed
uv run dm search "why does the electron have spin" --serendipity 0.5
uv run dm export-obsidian         # then open data/obsidian-vault in Obsidian
uv run pytest
```

Supabase Studio: http://127.0.0.1:54323. MCP endpoint: http://127.0.0.1:54321/mcp (registered in `.mcp.json`).

## Architecture

```
 Zitter BibTeX ─┐                      ┌─ OpenAlex / Crossref (enrichment)
                ▼                      ▼
        Python pipeline (uv, typer, psycopg) ── Qwen3 embeddings (local)
                │
                ▼
     Supabase (Postgres 17: pgvector, pg_trgm, full-text, RBAC, PROV-O)
                │
        ┌───────┴────────┐
        ▼                ▼
   Obsidian vault     Roam (next)
```

### Data model

Core table names and meanings follow upstream `packages/database` so ideas and data stay portable:

- **Space**: a collection (here: the Zitter Repository).
- **PlatformAccount**: a person or automated agent (our pipeline is an `automated_agent`).
- **Document / Content**: the paper and its searchable text (title + abstract).
- **Concept**: one table for node instances, relation instances, *and* their schemas
  (Source / Claim / Evidence / Question nodes; supports / opposes / informs / derived from / cites relations).
- **ContentEmbedding_qwen3_embedding_0_6b_1024**: one table per embedding model (upstream pattern),
  so models can be swapped without mixing incomparable vectors.

Deliberate differences: `Platform` also includes `Bibtex`; natural-key unique constraints make
every importer idempotent; keyword search uses Postgres full-text + trigram rather than pgroonga.

### Standards layers (ours)

| Layer | Standard | Tables |
|---|---|---|
| Persistent identifiers | DOI (ISO 26324), URI (RFC 3986), OpenAlex IDs | `external_identifier` |
| Provenance | W3C PROV-O (Activity, wasGeneratedBy, wasAssociatedWith) | `prov_activity`, `prov_generation` |
| Access control | NIST RBAC (roles → permissions, assigned per space) | `rbac_role`, `rbac_permission`, `rbac_role_permission`, `rbac_role_assignment`, `has_permission()` |
| Serendipity | Maximal Marginal Relevance, inspired by Stanley's novelty search | `search.mmr()` |

Every pipeline run is a PROV-O activity: which agent ran it, with which parameters, and which
rows it generated or revised. Ask "where did this abstract come from?" and the answer is in the database.

### Serendipity slider

`score = (1 − s) · relevance(query, doc) − s · max_similarity(doc, already_picked)`

At `s = 0` you get the closest matches. As `s` rises, results that resemble each other are pushed
down and different-but-relevant work moves up, e.g. a 1919 ring-electron paper next to a 2005
spin model. The candidate pool stays relevant to the query, so it's still a search, not a random walk.

## Layout

```
src/discourse_maps/   cli, zitter (fetch), bibtex, importer, enrich, embeddings, search, obsidian, db
supabase/migrations/  20261026000001_core.sql, 20261026000002_standards_layers.sql
tests/                unit tests + DB integration tests (marked `db`, skip if Supabase is down)
docs/design/          original concept, vision, and architecture documents
data/                 git-ignored: raw downloads, PDFs, generated Obsidian vault
```

## Roadmap

- [ ] Roam: import/export via the Roam backend API or JSON export
- [ ] Obsidian → Supabase direction (read hand-written Claim/Evidence notes back in)
- [ ] Enable RLS on core tables using `has_permission()`
- [ ] SKOS concept schemes for shared vocabularies (ISO 25964), and W3C Web Annotation
- [ ] HTTP API (FastAPI) mirroring upstream endpoints; nanopublication (RDF) export
- [ ] Claim/Evidence extraction from abstracts with an LLM, stored as *proposed* Concepts

## License

[Apache-2.0](LICENSE), same as upstream DiscourseGraphs/discourse-graph.
