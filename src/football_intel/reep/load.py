"""Load a downloaded Reep release's person and team bridges into `reference` (ADR 32, ADR 34)."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
from psycopg import AsyncConnection, sql
from psycopg.types.json import Jsonb

from football_intel.reep.release import Manifest

Bridge = tuple[str, str, str, str | None, str | None]  # reep_id, provider, external_id, rule, scope


@dataclass(frozen=True)
class Entity:
    """A Reep entity type we load: its namespace per provider we ingest, and its table."""

    name: str  # plural, for messages
    entity_type: str
    namespaces: tuple[tuple[str, str], ...]  # (provider, namespace)
    table: str


PERSONS = Entity(
    "persons",
    "player",
    (("sofascore", "player"), ("transfermarkt", "spieler"), ("understat", "player")),
    "reep_player_bridges",
)
# Never Transfermarkt's `team` namespace: its IDs are not the `verein` IDs we ingest.
TEAMS = Entity(
    "teams",
    "team",
    (("sofascore", "team"), ("transfermarkt", "verein"), ("understat", "team")),
    "reep_team_bridges",
)
ENTITIES = (PERSONS, TEAMS)
# A Reep entity bridged to fewer of the providers we ingest maps nothing for us (ADR 34).
MIN_PROVIDERS = 2


def bridges(directory: Path, entity: Entity) -> list[Bridge]:
    """The bridges of every `entity` that Reep maps to at least two of the providers we
    ingest, with each Reep ID resolved through the release's redirects to its survivor."""
    namespaces = " OR ".join(
        f"(provider = '{p}' AND namespace = '{n}')" for p, n in entity.namespaces
    )
    with duckdb.connect() as db:
        return [
            (
                str(r),
                str(p),
                str(e),
                None if rule is None else str(rule),
                None if scope is None else str(scope),
            )
            for r, p, e, rule, scope in db.execute(
                f"""
                WITH bridges AS (
                    SELECT DISTINCT COALESCE(redirects.to_id, bridges.reep_id) AS reep_id,
                        bridges.provider, bridges.external_id, bridges.rule_id, bridges.route_scope
                    FROM read_parquet(?) AS bridges
                    LEFT JOIN read_parquet(?) AS redirects ON redirects.from_id = bridges.reep_id
                    WHERE bridges.entity_type = ? AND ({namespaces})
                )
                SELECT reep_id, provider, external_id, rule_id, route_scope FROM bridges
                WHERE reep_id IN (
                    SELECT reep_id FROM bridges
                    GROUP BY reep_id HAVING count(DISTINCT provider) >= ?
                )
                ORDER BY reep_id, provider, external_id
                """,
                [
                    str(directory / "bridges.parquet"),
                    str(directory / "redirects.parquet"),
                    entity.entity_type,
                    MIN_PROVIDERS,
                ],
            ).fetchall()
        ]


async def load(
    conn: AsyncConnection[Any], manifest: Manifest, loaded: dict[Entity, list[Bridge]]
) -> None:
    """Replace a release's rows with the `loaded` bridges of each entity, in one transaction."""
    async with conn.transaction():
        await conn.execute(
            "DELETE FROM reference.reep_releases WHERE release = %s", (manifest.release,)
        )
        await conn.execute(
            "INSERT INTO reference.reep_releases (release, gate_passed, manifest)"
            " VALUES (%s, %s, %s)",
            (manifest.release, manifest.gate_passed, Jsonb(manifest.data)),
        )
        for entity, rows in loaded.items():
            async with conn.cursor().copy(
                sql.SQL(
                    "COPY reference.{} (release, reep_id, provider, external_id, rule_id,"
                    " route_scope) FROM STDIN"
                ).format(sql.Identifier(entity.table))
            ) as copy:
                for bridge in rows:
                    await copy.write_row((manifest.release, *bridge))
    # Fresh statistics, or the planner joins the bridges by nested loop: minutes, not seconds.
    for entity in loaded:
        await conn.execute(sql.SQL("ANALYZE reference.{}").format(sql.Identifier(entity.table)))


def manifest_from(directory: Path, release: str) -> Manifest:
    return Manifest(release, json.loads((directory / "manifest.json").read_text()))
