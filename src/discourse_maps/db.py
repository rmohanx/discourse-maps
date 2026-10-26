"""Database helpers: connections, the importer agent, and PROV-O activity tracking."""

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from discourse_maps.config import get_settings

AGENT_LOCAL_ID = "discourse-maps:pipeline"


def connect() -> psycopg.Connection:
    return psycopg.connect(get_settings().database_url, row_factory=dict_row)


def ensure_space(conn: psycopg.Connection, url: str, name: str, platform: str) -> int:
    row = conn.execute(
        """
        insert into "Space" (url, name, platform) values (%s, %s, %s)
        on conflict (url) do update set name = excluded.name
        returning id
        """,
        (url, name, platform),
    ).fetchone()
    return row["id"]


def ensure_agent(conn: psycopg.Connection, platform: str = "Bibtex") -> int:
    """The automated agent that runs our pipelines (prov:SoftwareAgent)."""
    row = conn.execute(
        """
        insert into "PlatformAccount" (name, platform, account_local_id, agent_type)
        values ('discourse-maps pipeline', %s, %s, 'automated_agent')
        on conflict (platform, account_local_id) do update set active = true
        returning id
        """,
        (platform, AGENT_LOCAL_ID),
    ).fetchone()
    return row["id"]


class Activity:
    """One PROV-O activity: records which entities a pipeline run generated or revised."""

    def __init__(self, conn: psycopg.Connection, activity_id: int):
        self.conn = conn
        self.id = activity_id
        self.summary: dict[str, int] = {}

    def record(self, entity_id: int, inserted: bool) -> None:
        """After an upsert: new rows are prov:wasGeneratedBy this activity; existing ones are left alone."""
        if inserted:
            self._link(entity_id, "generated")
        else:
            self.count("existing")

    def revised(self, entity_id: int) -> None:
        self._link(entity_id, "revised")

    def _link(self, entity_id: int, role: str) -> None:
        self.conn.execute(
            """
            insert into prov_generation (entity_id, activity_id, role) values (%s, %s, %s)
            on conflict do nothing
            """,
            (entity_id, self.id, role),
        )
        self.count(role)

    def count(self, key: str, n: int = 1) -> None:
        self.summary[key] = self.summary.get(key, 0) + n


@contextmanager
def activity(
    conn: psycopg.Connection, activity_type: str, agent_id: int, parameters: dict
) -> Iterator[Activity]:
    """Run a block as a PROV-O activity; the whole block commits or rolls back together."""
    row = conn.execute(
        """
        insert into prov_activity (activity_type, agent_id, parameters)
        values (%s, %s, %s) returning id
        """,
        (activity_type, agent_id, Jsonb(parameters)),
    ).fetchone()
    conn.commit()
    act = Activity(conn, row["id"])
    try:
        yield act
    except BaseException as exc:
        conn.rollback()
        conn.execute(
            """
            update prov_activity set status = 'failed', ended_at = now(), summary = %s
            where id = %s
            """,
            (Jsonb({**act.summary, "error": repr(exc)}), act.id),
        )
        conn.commit()
        raise
    conn.execute(
        "update prov_activity set status = 'complete', ended_at = now(), summary = %s where id = %s",
        (Jsonb(act.summary), act.id),
    )
    conn.commit()
