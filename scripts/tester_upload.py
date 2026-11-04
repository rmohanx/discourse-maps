#!/usr/bin/env python3
"""Upload discourse nodes from your own Obsidian vault to the hosted discourse-maps API.

Standalone: Python 3.10+, standard library only. Runs as YOU (your sign-in token), so row-level
security decides what is allowed. Nothing here bypasses it.

Reads the Discourse Graph plugin format:
  - notes with frontmatter `nodeTypeId`      -> one node per note
  - lines tagged with a node type's tag      -> one candidate node per line (e.g. `#clm-candidate`)

Usage:
  python3 tester_upload.py VAULT --space 1 --email you@example.com            # dry run (default)
  python3 tester_upload.py VAULT --space 1 --email you@example.com --send     # really upload
Password: --password, or the DM_PASSWORD environment variable, or a prompt.
"""

import argparse
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://vkmgormzbapxzdjmugmt.supabase.co"
KEY = "sb_publishable_j6F0JGmwbh_wBKs7qS4XMA_Wp4xm9tZ"  # public by design
PLUGIN_SETTINGS = Path(".obsidian/plugins/discourse-graphs/data.json")


def call(method, path, token=None, body=None, headers=None):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body else None)
    req.add_header("apikey", KEY)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode()
            return resp.status, json.loads(text) if text else None
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode()


def frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    fields = {}
    for line in m[1].splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip().strip("'\"")
    return fields, m[2]


def read_vault(vault):
    settings = json.loads((vault / PLUGIN_SETTINGS).read_text())
    types = {t["id"]: t for t in settings.get("nodeTypes", [])}
    by_tag = {t["tag"]: t for t in types.values() if t.get("tag")}
    nodes = []
    for path in sorted(vault.rglob("*.md")):
        if ".obsidian" in path.parts:
            continue
        rel = path.relative_to(vault).as_posix()
        fields, body = frontmatter(path.read_text())
        if fields.get("nodeTypeId") in types:
            nodes.append({"type": types[fields["nodeTypeId"]]["name"], "name": path.stem,
                          "body": body.strip(), "key": rel, "kind": "note"})
        for n, line in enumerate(body.splitlines(), 1):
            for tag, t in by_tag.items():
                if re.search(rf"#{re.escape(tag)}\b", line):
                    text = re.sub(rf"\s*#{re.escape(tag)}\b", "", line).strip(" -*\t")
                    nodes.append({"type": t["name"], "name": t["format"].replace("{content}", text),
                                  "body": line.strip(), "key": f"{rel}#L{n}", "kind": "tagged line"})
    return nodes


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("vault", type=Path)
    ap.add_argument("--space", type=int, required=True, help="Space id (see docs/tester-upload.md)")
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", default=os.environ.get("DM_PASSWORD"))
    ap.add_argument("--send", action="store_true", help="really upload (default is a dry run)")
    args = ap.parse_args()

    nodes = read_vault(args.vault.expanduser())
    print(f"found {len(nodes)} nodes in {args.vault}")

    password = args.password or getpass.getpass("password: ")
    status, auth = call("POST", "/auth/v1/token?grant_type=password",
                        body={"email": args.email, "password": password})
    if status != 200:
        sys.exit(f"sign-in failed: {status} {auth}")
    token = auth["access_token"]
    _, me = call("GET", "/rest/v1/PlatformAccount?select=id,name", token)
    account = me[0]["id"]
    _, schemas = call("GET", f"/rest/v1/Concept?select=id,name&space_id=eq.{args.space}"
                             "&schema_id=is.null&is_relation=eq.false", token)
    type_ids = {s["name"]: s["id"] for s in schemas}
    print(f"signed in as {me[0]['name']} (account {account}); space {args.space} types: {sorted(type_ids)}")

    sent = skipped = 0
    for node in nodes:
        schema = type_ids.get(node["type"])
        label = f"[{node['kind']}] {node['type']}: {node['name'][:70]}"
        if schema is None:
            print(f"  skip (type not in space) {label}")
            skipped += 1
            continue
        if not args.send:
            print(f"  would send {label}")
            continue
        row = {"name": node["name"], "space_id": args.space, "schema_id": schema, "author_id": account,
               "source_local_id": f"tester:{node['key']}",
               "literal_content": {"body": node["body"][:5000], "review_status": "candidate",
                                   "proposed_by": "tester_upload.py"}}
        status, out = call("POST", "/rest/v1/Concept?on_conflict=space_id,source_local_id", token, row,
                           {"Prefer": "resolution=ignore-duplicates,return=minimal"})
        ok = status in (200, 201, 204)
        sent += ok
        print(f"  {'sent' if ok else f'FAILED {status} {out}'} {label}")
    print("dry run: nothing uploaded. Add --send to upload." if not args.send
          else f"done: {sent} sent, {skipped} skipped. Re-running is safe (duplicates are ignored).")


if __name__ == "__main__":
    main()
