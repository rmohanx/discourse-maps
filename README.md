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
| Obsidian in | `uv run dm import-obsidian <vault>` | A researcher's vault (Discourse Graph plugin format): nodes, review status, relations |
| Roam in | `uv run dm import-roam` | A Roam graph via the backend API (or `--export file.json`); relations inferred from outline structure |
| Share → Obsidian | `uv run dm share-to-obsidian --from <space> --vault <vault>` | Return trip: another space's nodes + relations as shared copies in a vault |
| Share → Roam | `uv run dm share-to-roam --from <space>` | Return trip: pages + `[[SupportedBy]]` structure in the Roam graph |
| History | `uv run dm history "<title>"` | Every recorded version of a node and the run that replaced it |
| Conflicts | `uv run dm conflicts` | Mirrors a receiver edited after which the original changed |
| Spaces | `uv run dm spaces` | Every source (Roam graph, vault, import) with node and relation counts |
| Fetch | `uv run dm fetch` | Downloads the Zitter BibTeX files and paper list into `data/` (git-ignored) |
| Import | `uv run dm import` | BibTeX → `Document` + `Content` + `Concept` (Source node). Idempotent. |
| Enrich | `uv run dm enrich` | OpenAlex / Crossref: abstracts, DOIs, topics, citation counts, and **`cites` relations** between papers |
| Embed | `uv run dm embed` | Local **Qwen3-Embedding-0.6B** vectors (Apple GPU via MPS); only new or changed rows |
| Search | `uv run dm search "..."` | `--mode keyword \| fuzzy \| semantic`, plus `--serendipity 0..1` |
| Obsidian out | `uv run dm export-obsidian` | Zitter Sources as notes with frontmatter and `[[wiki-links]]` (secondary) |
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
uv run pytest                     # 29 tests; see docs/testing.md for each case and manual checks
```

Supabase Studio: http://127.0.0.1:54323. MCP endpoint: http://127.0.0.1:54321/mcp (registered in `.mcp.json`).

## Architecture

```
 Researcher's Obsidian vault ─┐        ┌─ Zitter BibTeX ─▶ OpenAlex / Crossref enrichment
 Researcher's Roam graph ─────┤        │
                              ▼        ▼
              Python pipeline (uv, typer, psycopg) ── Qwen3 embeddings (local)
                              │
                              ▼
   Supabase (Postgres 17): one schema for every tool · pgvector · FTS · PROV-O · RBAC + RLS
                              │
                              ▼
        HTTP API (PostgREST + Auth JWT): remote users see only what their role allows
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
| Access control | NIST RBAC (roles → permissions, assigned per space) | `rbac_role`, `rbac_permission`, `rbac_role_permission`, `rbac_role_assignment`, `private.has_permission()` |
| Serendipity | Maximal Marginal Relevance, inspired by Stanley's novelty search | `search.mmr()` |

Every pipeline run is a PROV-O activity: which agent ran it, with which parameters, and which
rows it generated or revised. Ask "where did this abstract come from?" and the answer is in the database.

### Serendipity slider

`score = (1 − s) · relevance(query, doc) − s · max_similarity(doc, already_picked)`

At `s = 0` you get the closest matches. As `s` rises, results that resemble each other are pushed
down and different-but-relevant work moves up, e.g. a 1919 ring-electron paper next to a 2005
spin model. The candidate pool stays relevant to the query, so it's still a search, not a random walk.

## Importing researchers' notes

**Obsidian** (`dm import-obsidian ~/path/to/vault`) reads what the Discourse Graph plugin writes: node
types from `.obsidian/plugins/discourse-graphs/data.json`, notes with `nodeTypeId` / `nodeInstanceId`
frontmatter, and `relations.json`. Review state rides along: `status: candidate` and `proposed_by: <agent>`
mark LLM-proposed nodes; hand-written notes count as accepted.

**Roam** (`dm import-roam`, needs `ROAM_GRAPH` + `ROAM_API_TOKEN`) reads pages and blocks via Datalog.
Discourse nodes are pages titled `[[QUE]] - …`, `[[CLM]] - …`, `[[EVD]] - … - @Source`, `@Source`.
Relations are inferred from structure like the Roam extension does, e.g. on a Claim page a block
`[[SupportedBy]]` with a child linking an Evidence page means *Evidence supports Claim*.
`status::` and `proposed_by::` attributes carry review state.

Both map to the same tables (Document + Content + Concept, relations as relation Concepts), so one
query spans every tool. Sources that match a paper elsewhere (DOI or BibTeX key) point at it.

## The return trip: sharing between researchers

`dm share-to-obsidian` and `dm share-to-roam` write one space's nodes into another researcher's tool, so
work made in Roam shows up in an Obsidian vault and vice versa (`--status accepted` shares only reviewed work).
Shared copies are mirrors, marked like the Discourse Graph plugin marks imports (`importedFromRid`,
relations with `tentative: false`). **Repeat sync and conflicts follow [docs/sync.md](docs/sync.md)**:

- the **database keeps every version** (`entity_revision`, written by triggers and linked to the PROV-O
  activity that replaced it); tools show the latest with a banner: *latest version vN, history in the database*
- unedited mirrors **refresh** when the original changes, in Obsidian and Roam
- a mirror the receiver **edited** is never overwritten: the incoming version goes beside it
  (`Shared/<space>/_conflicts/` in Obsidian, a `dm_conflict::` block in Roam) and a conflict is recorded
- deleting a mirror stops it being re-created; deleting an original marks it deleted and flags its mirrors
- sync state lives in the database (`share_record`), so every decision is auditable

```
Maya's Roam ──import-roam──▶ Supabase ──share-to-obsidian──▶ Sam's vault
     ▲                          ▲                                │
     └────share-to-roam─────────┴──────────import-obsidian───────┘
```

## Remote access through the API (RLS)

Every public table has row-level security driven by `has_permission(space, permission)`.
`uv run python scripts/api_user_demo.py` plays a remote user: sign in over HTTP, get a JWT, and query
`/rest/v1/...`. A `reader` on the Zitter space sees only that space; proposing a Claim is refused until
the role is `contributor`, and even then only as `unknown`, in your own name.

## Local and cloud

The hosted Supabase project is the main copy; the local Docker database is a working copy.

- **Schema** goes one way, laptop to cloud: `supabase db push --remote --yes` (try `--dry-run` first; CLI v3 needs `--yes` for hosted changes).
- **Pipeline** runs on the laptop against either database: `uv run dm --target cloud <command>`
  (or `DM_TARGET=cloud`). Default is `local`. Cloud settings live in the git-ignored `.env.cloud`
  (`DATABASE_URL_CLOUD`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, ...).
- **Data** reaches the cloud by re-running the pipeline there (`import`, `enrich`, `embed`,
  `import-obsidian`, `import-roam`), not by copying the local database.
- **Refresh the laptop** from the cloud: `scripts/pull_cloud.sh --yes` (wipes local data first).
- Tests always use the local database (`tests/conftest.py` forces `DM_TARGET=local`).

## Layout

```
src/discourse_maps/   cli, zitter, bibtex, importer, obsidian_import, roam, share, enrich, embeddings, search, obsidian, db
supabase/migrations/  core tables, standards layers, RLS, RLS hardening, sync history
docs/sync.md          sync / repeat-sync / conflict scenarios and policies
docs/testing.md       every test case (automated and manual) and how to run it from the CLI
scripts/              api_user_demo.py (remote user over HTTP under RLS), pull_cloud.sh (cloud -> laptop)
tests/                unit tests + DB integration tests (marked `db`, skip if Supabase is down)
docs/design/          original concept, vision, and architecture documents
data/                 git-ignored: raw downloads, PDFs, generated Obsidian vault
```

## Roadmap

See [ROADMAP.md](ROADMAP.md).

## License

[Apache-2.0](LICENSE), same as upstream DiscourseGraphs/discourse-graph.
