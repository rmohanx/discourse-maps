# Roadmap

Single list of planned work for discourse-maps. Update it when an item is done or added.
Status: `[x]` done, `[ ]` open, `[~]` in progress.

## Priorities

1. [ ] List and extend test cases and use cases (see *Tests and use cases*).
2. [~] Move Supabase to the cloud (see *Hosting*).
3. [ ] Implement and extend PROV-O (see *Provenance and standards*).
4. [ ] Implement SKOS (see *Provenance and standards*).
5. [ ] Set up a public API for Supabase (see *API and search*).
6. [ ] Create canned demos (see *Demos*).

## Done

- [x] BibTeX import (Zitter repository), OpenAlex/Crossref enrichment, `cites` relations
- [x] Local embeddings (Qwen3-Embedding-0.6B, 1024 dims); keyword, fuzzy, semantic, and serendipity search
- [x] Roam import via the backend API or JSON export
- [x] Obsidian vault import (Discourse Graph plugin format, review status)
- [x] Custom node and relation types (e.g. Hypothesis)
- [x] RLS on all tables using `has_permission()`; remote API user demo
- [x] Return trip: share nodes into another researcher's vault or Roam graph
- [x] Repeat sync: refresh, conflict detection, deletions, full history (`docs/sync.md`)
- [x] PROV-O run-level provenance (`prov_activity`, `prov_generation`, `entity_revision`)

## Data hygiene

- [ ] Orphaned provenance: DB tests delete their Space but leave `prov_activity` / `prov_generation`
      rows (63% of `prov_generation` rows were orphans on 2026-11-04). Fix test teardown, add a delete
      trigger (or periodic purge) on Document / Content / Concept, and clean existing orphans once.

## Tests and use cases

- [ ] Write down the use cases (researcher, lab, reviewer, LLM agent) in `docs/use-cases.md`, each linked
      to the tests that cover it.
- [ ] List the existing tests by area (import, enrich, search, share, sync, RLS) and find gaps.
- [ ] Add tests for the gaps: multi-user RLS, cross-space relations, provenance per author, API endpoints.
- [ ] Consider Gherkin feature files for access and concepts, like upstream.

## Provenance and standards

- [ ] Extend PROV-O beyond run-level records (items below), with queries that answer
      "who said what, when, from which source".

- [ ] Per-researcher provenance: record each author as a `prov:Agent` (`wasAttributedTo`) per Concept
      and per revision, not only the pipeline account on the run.
- [ ] Multiple contributors per Content / Concept (upstream has `content_contributors`, `concept_contributors`).
- [ ] W3C Web Annotation: comments and highlights on Content, with provenance.
- [ ] SKOS concept schemes for shared vocabularies (ISO 25964); map node and relation types to SKOS.
- [ ] RDF export of the graph (PROV-O + SKOS); nanopublication export.
- [ ] Ontology mapping: link our types to external vocabularies (e.g. OBO / LinkML terms).

## Access and collaboration

- [ ] Groups (upstream `group_membership`) and per-resource access (upstream `ResourceAccess`).
- [ ] Agent identifiers per account (email, ORCID) like upstream `AgentIdentifier`.
- [ ] Share links / invites with expiring one-time tokens (upstream `secret_token`).

## Sync and tools

- [ ] Upstream contribution: configurable server endpoint in the Obsidian and Roam plugins. Today
      `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, and `NEXT_API_ROOT` are baked in at build time
      (esbuild `define` in `apps/obsidian/scripts/compile.ts`, `apps/roam/scripts/compile.ts`), so every
      published build talks only to the hosted backend. Add a settings field (URL + key, defaulting to the
      built-in values) to allow self-hosted labs, staging, and other servers without a rebuild.
      Follow-up: federation (sharing between separate servers).
- [ ] Trigger import from inside the tools: Obsidian hotkey or on-save via a shell-command plugin;
      Roam extension calling our API.
- [ ] Sync job lock (upstream `sync_info`) for concurrent or scheduled syncs.
- [ ] Attachments: file references by hash with garbage collection (upstream `FileReference`, `file_gc`).
- [ ] Sanitize imports (page refs, slashes in titles); templates and slots in schema and API.

## API and search

- [ ] Public API on hosted Supabase: PostgREST under RLS, API keys per user, documented endpoints.
- [ ] HTTP API (FastAPI) mirroring upstream endpoints (`/content`, `/space`, `/similarity-rank`).
- [ ] Semantic search as an API endpoint (RPC or Edge Function).
- [ ] `my_*` views for API clients, like upstream.
- [ ] Full-text PDF search (e.g. "who mentions stochastic electrodynamics?").
- [ ] Claim / Evidence extraction from abstracts with an LLM, stored as *proposed* Concepts.

## Hosting

Hosted project on the Free plan (us-east-1). Free pauses after 1 week of inactivity; Pro + Micro is
$25/month flat and upgrades in place.

- [x] Create project, `supabase link`, `supabase db push --remote` (RLS on all 16 tables).
- [x] `dm --target local|cloud`; tests forced to local.
- [x] Load cloud by re-running the pipeline (224 documents, 269 relations, 224 embeddings).
- [x] `scripts/pull_cloud.sh` to refresh the laptop from the cloud.
- [x] Advisors on the cloud project (Management API): Security 0 findings after migration 6; only "unused index" info notes remain.
- [x] Auth: open sign-ups disabled; tester account (contributor on all 3 spaces); `docs/api-tester.md`.
- [ ] Auth: set site URL and redirect URLs (still `http://localhost:3000`).
- [x] Database password reset to a generated one (2026-11-04).
- [ ] Turn off `pg_graphql` unless needed; keep auth rate limits on.
- [ ] Shared-copy ids (`importedFromRid` = `dm:<space>#<id>`) use database ids, so copies made against
      the local database do not resolve in the cloud (7 orphaned on first cloud import). Use stable keys
      (space URL + `source_local_id`) instead.
- [ ] Server-side query embeddings for API search (built-in `gte-small` Edge Function, or OpenAI).

## Demos

- [ ] Canned demos: scripted, repeatable, resettable runs (e.g. `scripts/demo_*.sh` with seed data):
      import from Obsidian and Roam, cross-tool search, share and re-sync with a conflict, provenance
      trail for one claim, remote API user under RLS.
- [ ] A reset step so each demo starts from the same state.
