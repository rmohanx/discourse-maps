"""Demo: a remote user talks to the database only through Supabase's HTTP API, under RLS.

    uv run python scripts/api_user_demo.py

1. Setup (admin side, direct DB + Auth admin API): create a user, link a PlatformAccount,
   grant `reader` on the Zitter space only.
2. Remote side (HTTP + public key only): sign in, get a JWT, query REST endpoints.
3. Show what RLS allows and refuses; upgrade to `contributor` and propose a Claim.
Local Supabase only: keys come from `supabase status`.
"""

import json
import subprocess

import httpx

from discourse_maps.db import connect
from discourse_maps.importer import ZITTER_SPACE

EMAIL, PASSWORD = "remote.reader@example.com", "demo-password-123"


def local_keys() -> dict:
    out = subprocess.run(
        ["supabase", "status", "-o", "json"], capture_output=True, text=True, check=True
    ).stdout
    return json.loads(out[out.index("{") :])


def step(title: str) -> None:
    print(f"\n=== {title}")


def show(resp: httpx.Response, limit: int = 300) -> None:
    print(f"HTTP {resp.status_code}: {resp.text[:limit]}")


def main() -> None:
    keys = local_keys()
    api, public_key, service_key = keys["API_URL"], keys["ANON_KEY"], keys["SERVICE_ROLE_KEY"]
    http = httpx.Client(timeout=30)

    step("Admin: create the auth user (idempotent)")
    admin = {"apikey": service_key, "Authorization": f"Bearer {service_key}"}
    resp = http.post(
        f"{api}/auth/v1/admin/users",
        headers=admin,
        json={"email": EMAIL, "password": PASSWORD, "email_confirm": True},
    )
    print("created" if resp.status_code == 200 else f"exists or skipped ({resp.status_code})")

    with connect() as conn:
        user_id = conn.execute("select id from auth.users where email = %s", (EMAIL,)).fetchone()["id"]
        account = conn.execute(
            """
            insert into "PlatformAccount" (name, platform, account_local_id, agent_type, dg_account)
            values ('Remote reader (demo)', 'Bibtex', %s, 'person', %s)
            on conflict (platform, account_local_id) do update set dg_account = excluded.dg_account
            returning id
            """,
            (f"auth:{EMAIL}", user_id),
        ).fetchone()["id"]
        zitter = conn.execute('select id from "Space" where url = %s', (ZITTER_SPACE[0],)).fetchone()["id"]
        conn.execute("delete from rbac_role_assignment where account_id = %s", (account,))
        conn.execute(
            "insert into rbac_role_assignment (account_id, space_id, role_name) values (%s, %s, 'reader')",
            (account, zitter),
        )
        conn.commit()
    print(f"user {user_id} -> PlatformAccount {account} -> role 'reader' on space {zitter} (Zitter only)")

    step("Remote: anonymous request with only the public key")
    anon = {"apikey": public_key}
    show(http.get(f"{api}/rest/v1/Concept?select=name&limit=3", headers=anon))

    step("Remote: sign in with email + password -> JWT")
    token = (
        http.post(
            f"{api}/auth/v1/token?grant_type=password",
            headers=anon,
            json={"email": EMAIL, "password": PASSWORD},
        )
        .raise_for_status()
        .json()["access_token"]
    )
    print(f"got access token ({len(token)} chars)")
    user = {"apikey": public_key, "Authorization": f"Bearer {token}"}

    step("Remote: which spaces can I see?")
    show(http.get(f"{api}/rest/v1/Space?select=id,name,platform", headers=user))

    step("Remote: count Concepts I can read (Zitter only; the Obsidian and Roam spaces stay hidden)")
    resp = http.get(
        f"{api}/rest/v1/Concept?select=id", headers={**user, "Prefer": "count=exact", "Range": "0-0"}
    )
    print("visible concepts:", resp.headers.get("content-range"))

    step("Remote: query Sources about 'spin' with their citation counts")
    show(
        http.get(
            f"{api}/rest/v1/Document",
            params={
                "select": "metadata->>title,metadata->openalex->>cited_by_count",
                "metadata->>title": "ilike.*spin*",
                "limit": "4",
            },
            headers=user,
        ),
        600,
    )

    step("Remote: try to propose a Claim as a reader (should be refused by RLS)")
    claim = {
        "name": "CLM - (remote demo) Zitterbewegung should show up in electron channeling",
        "space_id": zitter,
        "author_id": account,
        "source_local_id": "remote-demo:claim-1",
    }
    show(http.post(f"{api}/rest/v1/Concept", headers=user, json=claim))

    step("Admin: upgrade the user to 'contributor' on Zitter")
    with connect() as conn:
        conn.execute(
            "update rbac_role_assignment set role_name = 'contributor' where account_id = %s", (account,)
        )
        conn.commit()

    step("Remote: propose the same Claim again (allowed: contributors may propose)")
    show(http.post(f"{api}/rest/v1/Concept", headers={**user, "Prefer": "return=representation"}, json=claim))

    step("Remote: try to propose in someone else's name (refused: author must be your own account)")
    show(
        http.post(
            f"{api}/rest/v1/Concept",
            headers=user,
            json={**claim, "source_local_id": "remote-demo:claim-3", "author_id": 1},
        )
    )

    step("Remote: try to propose a Claim already marked 'certain' (refused: proposals start unknown)")
    show(
        http.post(
            f"{api}/rest/v1/Concept",
            headers=user,
            json={**claim, "source_local_id": "remote-demo:claim-2", "epistemic_status": "certain"},
        )
    )

    step("Cleanup: remove the demo Claim and reset the role to reader")
    with connect() as conn:
        conn.execute("""delete from "Concept" where source_local_id like 'remote-demo:%%'""")
        conn.execute("update rbac_role_assignment set role_name = 'reader' where account_id = %s", (account,))
        conn.commit()
    print("done")


if __name__ == "__main__":
    main()
