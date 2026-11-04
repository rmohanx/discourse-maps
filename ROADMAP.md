# Roadmap

Single list of planned work for discourse-maps. Update it when an item is done or added.
Status: `[x]` done, `[ ]` open, `[~]` in progress.

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

## Provenance and standards

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

- [ ] Trigger import from inside the tools: Obsidian hotkey or on-save via a shell-command plugin;
      Roam extension calling our API.
- [ ] Sync job lock (upstream `sync_info`) for concurrent or scheduled syncs.
- [ ] Attachments: file references by hash with garbage collection (upstream `FileReference`, `file_gc`).
- [ ] Sanitize imports (page refs, slashes in titles); templates and slots in schema and API.

## API and search

- [ ] HTTP API (FastAPI) mirroring upstream endpoints (`/content`, `/space`, `/similarity-rank`).
- [ ] Semantic search as an API endpoint (RPC or Edge Function).
- [ ] `my_*` views for API clients, like upstream.
- [ ] Full-text PDF search (e.g. "who mentions stochastic electrodynamics?").
- [ ] Claim / Evidence extraction from abstracts with an LLM, stored as *proposed* Concepts.

## Hosting

- [ ] Move to hosted Supabase. Database is ~19 MB, so Free (Nano) fits, but Free pauses after 1 week
      of inactivity; Pro + Micro is $25/month flat. Steps: `supabase link`, `supabase db push`, load data.
