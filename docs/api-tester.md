# API quick start for testers

A hosted Supabase project with discourse-graph data (Zitter physics papers, an Obsidian vault, a Roam graph).
Access is invite-only and governed by row-level security: you see and write only what your role allows.

## Endpoint

| | |
|---|---|
| Base URL | `https://vkmgormzbapxzdjmugmt.supabase.co` |
| REST API | `<base>/rest/v1/<Table>` (PostgREST) |
| Publishable key | `sb_publishable_j6F0JGmwbh_wBKs7qS4XMA_Wp4xm9tZ` (public by design) |
| Account | email and password are sent to you separately |

## 1. Sign in

```bash
B=https://vkmgormzbapxzdjmugmt.supabase.co
K=sb_publishable_j6F0JGmwbh_wBKs7qS4XMA_Wp4xm9tZ
T=$(curl -s -X POST "$B/auth/v1/token?grant_type=password" -H "apikey: $K" \
  -H "Content-Type: application/json" -d '{"email":"YOUR_EMAIL","password":"YOUR_PASSWORD"}' | jq -r .access_token)
```

Send `apikey: $K` and `Authorization: Bearer $T` on every request. The token expires after 1 hour; sign in again.

## 2. Read

```bash
# Your account id (needed to write)
curl -s "$B/rest/v1/PlatformAccount?select=id,name" -H "apikey: $K" -H "Authorization: Bearer $T"
# Spaces you can see
curl -s "$B/rest/v1/Space?select=id,name" -H "apikey: $K" -H "Authorization: Bearer $T"
# Nodes in a space (Claims, Evidence, Sources, Questions)
curl -s "$B/rest/v1/Concept?select=id,name,epistemic_status&space_id=eq.1&is_relation=eq.false&limit=10" \
  -H "apikey: $K" -H "Authorization: Bearer $T"
# Papers with OpenAlex citation counts
curl -s "$B/rest/v1/Document?select=metadata->>title,metadata->openalex->>cited_by_count&limit=5" \
  -H "apikey: $K" -H "Authorization: Bearer $T"
```

## 3. Propose a Claim

`schema_id` is the space's `Claim` node type (in space 1 it is `5`; find others with
`Concept?select=id,name,space_id&schema_id=is.null&is_relation=eq.false`).

```bash
curl -s -X POST "$B/rest/v1/Concept" -H "apikey: $K" -H "Authorization: Bearer $T" \
  -H "Content-Type: application/json" -H "Prefer: return=representation" \
  -d '{"name":"CLM - my claim","space_id":1,"schema_id":5,"author_id":YOUR_ACCOUNT_ID,"source_local_id":"tester:claim-1"}'
```

## Expected refusals (worth testing)

| Try | Result |
|---|---|
| Any request without signing in | empty result `[]` |
| Sign up a new account (`/auth/v1/signup`) | refused (invite-only) |
| Propose with someone else's `author_id` | `403` row-level security |
| Propose with `"epistemic_status":"certain"` | `403` (proposals start as `unknown`) |
| Call `/rest/v1/rpc/has_permission` | `404` (not exposed) |

Data is test data; feel free to write. Please prefix `source_local_id` with `tester:` so test rows are easy to find.
