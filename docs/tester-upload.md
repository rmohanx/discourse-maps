# Upload test: push your own Obsidian nodes

**Why:** check that an outside researcher can add discourse nodes from *their own* vault to the shared
database, under their own name, and that row-level security (RLS) holds. You only need Python and the
login we sent you. Read-only basics are in [api-tester.md](api-tester.md).

## Data flow

```
your vault (.md files)  ->  tester_upload.py (your laptop)  ->  /rest/v1/Concept (signed in as you)
                                                                  -> RLS check -> shared Postgres
```

- Notes with frontmatter `nodeTypeId` (Discourse Graph plugin) become one node each.
- Lines tagged with a node type's tag (`#clm-candidate`, `#evd-candidate`, `#src-candidate`) become
  one candidate node each.
- Everything lands as a *proposal*: status `unknown`, author = you. Curators review it later.

## Run it

Needs Python 3.10+. No installs. Grab two things from the repo:
[`scripts/tester_upload.py`](../scripts/tester_upload.py) and, if you have no vault handy,
[`docs/samples/tester-vault/`](samples/tester-vault/).

```bash
# 1. dry run: shows what would be sent, uploads nothing
python3 tester_upload.py ~/path/to/your-vault --space 1 --email YOUR_EMAIL

# 2. real upload
python3 tester_upload.py ~/path/to/your-vault --space 1 --email YOUR_EMAIL --send
```

Password comes from `--password`, or `DM_PASSWORD`, or a prompt. Re-running is safe; duplicates are ignored.

**Spaces:** `1` Zitter Repository, `812` Obsidian vault, `992` Roam graph. Node types are matched by name
(Claim, Evidence, Source, Question); types the space doesn't have are skipped.

## Check it worked

```bash
B=https://vkmgormzbapxzdjmugmt.supabase.co; K=sb_publishable_j6F0JGmwbh_wBKs7qS4XMA_Wp4xm9tZ
T=$(curl -s -X POST "$B/auth/v1/token?grant_type=password" -H "apikey: $K" -H "Content-Type: application/json" \
  -d '{"email":"YOUR_EMAIL","password":"YOUR_PASSWORD"}' | jq -r .access_token)
curl -s "$B/rest/v1/Concept?select=name,epistemic_status,literal_content->>review_status&source_local_id=like.tester:*" \
  -H "apikey: $K" -H "Authorization: Bearer $T"
```

## Try to break it

- Edit the script to send another `author_id`, or `"epistemic_status": "certain"`: expect `403`.
- Send to a space you have no role in: expect `403`.

Your rows are tagged `source_local_id = tester:<file>[#L<line>]`, so they're easy to find and clean up.
