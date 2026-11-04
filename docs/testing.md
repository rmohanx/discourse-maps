# Test cases

How to run every test, automated or by hand, from the command line. Commands assume the repo root
(`cd discourse-maps`).

## Architecture under test

```
  Roam graph ──import-roam──┐                    ┌── Zitter BibTeX ── enrich ◀── OpenAlex / Crossref
  Obsidian vault ─import-obsidian─┤              │
                                  ▼              ▼
            Supabase (Postgres 17, local Docker)                       ◀── embed (Qwen3, local)
            ├─ data:    Space · Document · Content · Concept · ContentEmbedding_*
            ├─ control: rbac_* + has_permission() + RLS · prov_activity / prov_generation
            └─ sync:    share_record · sync_conflict · entity_revision (history)
                                  │                              │
          share-to-obsidian / share-to-roam                 HTTP API (PostgREST + Auth JWT)
                                  ▼                              ▼
                  other researchers' tools              remote users and agents
```

| Plane | What it covers | Where tested |
|---|---|---|
| **Data** | import, enrichment, sharing, repeat sync | `test_import_db`, `test_obsidian_import`, `test_roam`, `test_share` |
| **Control** | access rules (RBAC + RLS), provenance, sync ledger, history | `test_import_db`, `test_share`, manual M1–M3 |
| **Correctness** | parsers and converters (BibTeX, DOIs, Roam refs, titles) | `test_parsing`, `test_roam`, `test_share` |
| **Usage** | search and the serendipity slider | `test_search`, manual M5 |

## Setup (once)

```bash
supabase start                 # local Postgres + API + Auth in Docker
supabase migration up --local  # apply migrations (keeps data); a fresh clone can use `supabase db reset`
uv sync                        # Python dependencies
cp .env.example .env           # then fill in OPENALEX_API_KEY, ROAM_GRAPH, ROAM_API_TOKEN for live checks
```

Tests marked `db` need `supabase start`; without it they **skip** (not fail). Each `db` test works in
its own throwaway space (`test://…`, `*-test-*-dm`) and deletes it afterwards; your real data is untouched.
Vault tests copy fixtures to a temporary folder; Roam sharing tests use an in-memory fake graph.

## Running the automated suite

| Goal | Command |
|---|---|
| Everything (29 tests) | `uv run pytest` |
| Verbose, one line per test | `uv run pytest -v` |
| Only tests that need no database (16) | `uv run pytest -m "not db"` |
| Only database tests (13) | `uv run pytest -m db` |
| One file | `uv run pytest tests/test_share.py -v` |
| One test | `uv run pytest "tests/test_share.py::test_receiver_edit_is_never_overwritten" -v` |
| Stop at first failure, show prints | `uv run pytest -x -s` |
| Lint (part of "done") | `uv run ruff check .` |

## Automated test cases

Run any row with `uv run pytest "<test id>" -v`. **DB** = needs `supabase start`.

| # | Story | Plane | DB | Test id | Passes when |
|---|---|---|---|---|---|
| 1 | As a curator, I want re-running the BibTeX import to create no duplicates | data | ✓ | `tests/test_import_db.py::test_import_is_idempotent` | 2nd run reports `existing: 9`, no `generated`; 3 Documents / Contents / Concepts |
| 2 | As an auditor, I want every import recorded as a provenance activity | control | ✓ | `tests/test_import_db.py::test_import_records_provenance` | one `import:bibtex` activity, status `complete`, linked to 9 generated rows |
| 3 | As an admin, I want someone with no role denied by default | control | ✓ | `tests/test_import_db.py::test_has_permission_denies_without_role` | `has_permission(space, 'content:read')` is false |
| 4 | As a researcher, I want my vault's nodes, status and relations read correctly | correctness | | `tests/test_obsidian_import.py::test_read_vault_nodes_status_and_relations` | 3 nodes, 1 plain note skipped, relations from `relations.json` + legacy frontmatter |
| 5 | As a researcher, I want to upload my vault repeatedly without duplicates, keeping review status and proposer | data | ✓ | `tests/test_obsidian_import.py::test_import_vault_is_idempotent_and_keeps_review_state` | 2nd run generates nothing; LLM note stays `candidate` authored by `test-llm`; hand-written note `accepted` |
| 6 | As a researcher, I want no duplicates when the plugin assigns IDs later | data | ✓ | `tests/test_obsidian_import.py::test_plugin_assigning_an_id_later_does_not_duplicate` | adding `nodeInstanceId` to a note still leaves 3 nodes |
| 7 | As a lab, I want custom types (Hypothesis, "tests") imported, not crash | data | ✓ | `tests/test_obsidian_import.py::test_custom_node_and_relation_types_are_imported` | `hypothesis_accepted: 1`, `relation_tests: 1`; two custom schema Concepts |
| 8 | As a curator, I want BibTeX accents converted and DOIs normalized | correctness | | `tests/test_parsing.py::test_parse_bibtex_converts_latex_and_normalizes_doi` | `{\"U}ber` → `Über`; DOI URL → lowercase bare DOI |
| 9 | As a curator, I want every DOI spelling recognized | correctness | | `tests/test_parsing.py::test_normalize_doi_variants` | `doi:`, `dx.doi.org/`, mixed case all normalize |
| 10 | As a curator, I want title matching to ignore word order and case | correctness | | `tests/test_parsing.py::test_title_similarity_ignores_word_order_and_case` | "Metamaterial Toroidal" = "Toroidal metamaterial" (1.0) |
| 11 | As a reader, I want abstracts rebuilt from OpenAlex's word index | correctness | | `tests/test_parsing.py::test_abstract_rebuilt_from_inverted_index` | `{word: [positions]}` → original sentence |
| 12 | As a reader, I want Crossref XML abstracts cleaned | correctness | | `tests/test_parsing.py::test_strip_jats` | JATS tags and title removed |
| 13 | As a researcher, I want exported note titles to be valid filenames | correctness | | `tests/test_parsing.py::test_note_title_is_a_safe_filename` | `: " / ?` stripped |
| 14 | As a curator, I want the Zitter paper list parsed from the website | correctness | | `tests/test_parsing.py::test_parse_paper_list_rows` | author, year, Drive id extracted |
| 15 | As a Roam user, I want nested links and tags understood | correctness | | `tests/test_roam.py::test_extract_page_refs_handles_nesting_and_tags` | `[[[[EVD]] - x]]` yields `EVD` and `[[EVD]] - x`; `#tag` found |
| 16 | As a Roam user, I want node types recognized and relations inferred from my outline | correctness | | `tests/test_roam.py::test_classify_and_infer_relations` | 7 nodes; exactly 6 relations (informs, 2 supports, opposes, 2 derived from) |
| 17 | As a Roam user, I want `status::` / `proposed_by::` read as review state | correctness | | `tests/test_roam.py::test_page_attributes` | attributes parsed into a dict |
| 18 | As a Roam user, I want to upload my graph repeatedly without duplicates | data | ✓ | `tests/test_roam.py::test_import_graph_is_idempotent` | counts match the fixture; 2nd run generates nothing |
| 19 | As a searcher, I want serendipity 0 to rank plainly | usage | | `tests/test_search.py::test_serendipity_zero_is_plain_ranking` | order = closest first |
| 20 | As a searcher, I want higher serendipity to skip near-duplicates | usage | | `tests/test_search.py::test_serendipity_prefers_variety_over_near_duplicates` | at 0.7 the 2nd pick is the different item, not the near-duplicate |
| 21 | As a searcher, I want sane results when asking for more than exist | usage | | `tests/test_search.py::test_mmr_never_returns_more_than_available` | k=10 over 3 candidates returns 3 |
| 22 | As a sharer, I want titles converted between tool formats | correctness | | `tests/test_share.py::test_bare_content_strips_tool_prefixes` | `[[CLM]] - x`, `SRC - x`, `@x` → `x` |
| 23 | As a sharer, I want Roam's "Evidence – @Source" title convention | correctness | | `tests/test_share.py::test_roam_title_encodes_derived_from_in_evidence_title` | `[[EVD]] - … - @Huang 1952` |
| 24 | As Sam, I want Maya's Roam nodes in my vault, and re-import to create no duplicates (S1, S2, I6) | data | ✓ | `tests/test_share.py::test_first_share_and_round_trip_without_duplicates` | `create: 7`, `relations_written: 6`; re-share `unchanged: 7`; re-import `shared_copies_mapped: 7` |
| 25 | As Sam, I want my copy refreshed when Maya edits, with the old version in history (S3, I3) | data + control | ✓ | `tests/test_share.py::test_origin_edit_refreshes_mirror_and_keeps_history` | `refresh: 1`, `dm_version: 2`; one `entity_revision` row with the old text, linked to `import:roam` |
| 26 | As Sam, I want my edits never overwritten; conflict logged once (S4) | data + control | ✓ | `tests/test_share.py::test_receiver_edit_is_never_overwritten` | edit kept, `dm_status: conflict`, incoming note in `_conflicts/` (not a node), 1 open `sync_conflict`, no repeat |
| 27 | As Sam, I want a copy I deleted to stay deleted (S5) | data | ✓ | `tests/test_share.py::test_receiver_deletion_is_respected` | `deleted_by_receiver: 1`; file not re-created |
| 28 | As Sam, I want my copy flagged, not deleted, when Maya deletes her original (S6, I4) | data | ✓ | `tests/test_share.py::test_origin_deletion_flags_mirror_and_keeps_it` | import `marked_deleted ≥ 1`; share `origin_deleted: 1`; mirror kept with `dm_status: origin_deleted` |
| 29 | As Maya, I want Sam's vault work in my Roam graph across all scenarios | data | ✓ | `tests/test_share.py::test_share_to_roam_scenarios` | create 3 → unchanged 3 → refresh (v2) → conflict (her edit kept, `dm_conflict::` block) → deleted page not re-created |

Scenario codes (I1–I6, S1–S7) are defined in [sync.md](sync.md).

## Manual / live checks

These were run against live services during development; they are not in `pytest` because they need
the HTTP API with sign-in, OpenAlex, or a real Roam graph. **M6 and M7 write to your real vault and
Roam graph** (shared copies only; your own notes are never touched).

| # | Story | Plane | Command | Passes when |
|---|---|---|---|---|
| M1 | As a remote user, I want access governed by my role over HTTP | control | `uv run python scripts/api_user_demo.py` | anonymous `[]`; sees only "Zitter Repository"; reader proposal `403`; contributor `201` with `epistemic_status: unknown`; someone else's name `403`; `certain` `403` |
| M2 | As an anonymous caller, I want nothing | control | step 2 of M1, or `curl -s "http://127.0.0.1:54321/rest/v1/Concept?select=id&limit=1" -H "apikey: $(supabase status -o json 2>/dev/null \| jq -r .ANON_KEY)"` | `[]` |
| M3 | As an operator, I want no security findings | control | `supabase db advisors --local` | `"results":[]` |
| M4 | As a curator, I want enrichment to stop cleanly on rate limits and resume | data | `uv run dm enrich` (without `OPENALEX_API_KEY`, after the shared daily budget is spent) | prints `stopped: … retry after Ns`; finished papers are kept; next run continues |
| M5 | As a researcher, I want one search across all tools | usage | `uv run dm search "is the electron a point or does it have a size" --k 8` | results tagged `Roam`, `Obsidian`, `Bibtex` in one ranking |
| M6 | As Sam, I want Maya's live Roam graph in my vault, idempotently | data | `uv run dm import-roam` then `uv run dm share-to-obsidian --from roam://graph/$ROAM_GRAPH --vault <vault>` twice | 1st: `create`/`refresh`; 2nd: `unchanged: N` |
| M7 | As Maya, I want Sam's accepted vault nodes in my live Roam graph, idempotently | data | `uv run dm share-to-roam --from obsidian://vault/<vault-folder-name> --status accepted` twice, then `uv run dm import-roam` | 2nd share `unchanged`; import shows `shared_copies_mapped`, no `generated` |
| M8 | As Sam, I want to see a live conflict end to end | data + control | 1) add a line to a note in `<vault>/Shared/…`; 2) edit the original in Roam; 3) `uv run dm import-roam`; 4) share as in M6; 5) `uv run dm conflicts`; 6) `uv run dm history "<title>"` | share reports `conflict: 1`; your line still there; incoming copy in `Shared/…/_conflicts/`; history lists the old version and the run that replaced it |
| M9 | As an operator, I want local Supabase to fit in a 4 GB Docker VM | ops | `docker stats --no-stream` | total container memory well under 4 GB (≈ 0.6 GB observed) |

## Fixtures

| File | Used by | Contains |
|---|---|---|
| `tests/fixtures/sample.bib` | 1, 2, 8 | 3 BibTeX entries (DOI URL, LaTeX accents, a book) |
| `tests/fixtures/vault/` | 4–7, 24–29 | a minimal vault: plugin `data.json`, Claim (no id), Evidence (candidate, LLM), Source, plain note, `relations.json` |
| `tests/fixtures/roam_export.json` | 15–18, 24–28 | a Roam export: Question, 2 Claims, 2 Evidence, 2 Sources, `[[SupportedBy]]`/`[[OpposedBy]]` structure, attributes, a daily note |
