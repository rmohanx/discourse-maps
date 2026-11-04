# Sync, repeat sync, and conflicts

Three places hold discourse nodes: researchers' tools (Roam graphs, Obsidian vaults) and the database.
Moves between them:

- **import**: tool → database (`dm import-roam`, `dm import-obsidian`)
- **share**: database → another researcher's tool (`dm share-to-obsidian`, `dm share-to-roam`)

## Principles

1. **One owner per node.** A node belongs to the space it was written in. Copies in other tools are
   *mirrors*: read-only by convention, marked with `importedFromRid` (a pointer to the original).
2. **The database keeps history; tools show the latest.** Every change to a node's text, title, or
   status is kept in `entity_revision`, written by a trigger, so nothing is lost even when a tool is
   overwritten. Mirrors carry a banner: *latest version vN, full history in the database*.
3. **Never silently overwrite a person's edit.** If a receiver edited a mirror, the next share leaves their
   edit in place, writes the incoming version next to it, and records a conflict.
4. **Respect deletions, never propagate them destructively.** Deleting a mirror stops it being re-created;
   deleting an original marks it deleted in the database and flags its mirrors, but deletes nothing.
5. **Sync state lives in the database** (`share_record`), not in the tools, so every decision is
   reproducible and auditable (each run is a PROV-O activity).

## Scenarios

| # | Situation | What happens | Recorded in the database |
|---|---|---|---|
| I1 | First import of a node | Document + Content + Concept created | `prov_generation` (generated) |
| I2 | Repeat import, nothing changed | No-op | nothing new |
| I3 | Repeat import, author edited the node | Rows updated in place | old version in `entity_revision`, linked to the import activity |
| I4 | Node deleted in its home tool | Concept marked `deleted_at` (kept, not removed) | revision of the pre-deletion state |
| I5 | Deleted node reappears | `deleted_at` cleared | revision |
| I6 | Importing a mirror (someone else's node) | Mapped to the original; nothing duplicated | nothing new |
| S1 | First share of a node into a tool | Mirror created with banner, version, and hash | `share_record` (active) |
| S2 | Re-share, origin unchanged, mirror untouched | No-op | nothing new |
| S3 | Re-share, origin changed, mirror untouched | Mirror refreshed to the latest version | `share_record` updated |
| S4 | Re-share, receiver edited the mirror | Receiver's edit kept; incoming version written beside it; mirror flagged | `sync_conflict` (open), `share_record` = conflict |
| S5 | Receiver deleted the mirror | Not re-created | `share_record` = deleted_by_receiver |
| S6 | Origin deleted | Mirror kept, flagged "original deleted" | `share_record` = origin_deleted |
| S7 | Receiver adds a relation to a mirror | Imported as the receiver's relation, pointing at the original node | relation Concept in the receiver's space |

"Edited" is detected by hashing the mirror's body when it is written (`share_record.shared_hash`) and
comparing on the next share. Relations the receiver adds (Obsidian `relations.json`, Roam
`[[SupportedBy]]` blocks) are not edits of the mirror's content.

## What a receiver sees

- **Obsidian:** frontmatter `dm_version`, `dm_synced_at`, `dm_status`, and a banner line at the top of
  the note. Conflicts: the incoming version as a plain (non-node) note in `Shared/<space>/_conflicts/`.
- **Roam:** attribute blocks `dm_version::`, `dm_synced_at::`, `dm_status::`, and a banner block.
  Conflicts: a `dm_conflict::` block on the page with the incoming version nested under it.

## Inspecting history and conflicts

- `dm history "<title or rid>"`: every recorded version of a node, newest first, with the activity that replaced it.
- `dm conflicts`: open conflicts across all tools.

## Not handled (on purpose, for now)

- Merging a receiver's edit back into the original (mirrors are read-only; contribute by creating your
  own node and relating it).
- Live or concurrent co-editing of one node (would need CRDTs).
- History *inside* the tools; it lives in the database and is one command away.
