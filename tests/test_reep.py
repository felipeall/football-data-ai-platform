"""The Reep loader and the Reep step of Identity Resolution, on a synthetic release. No Reep
data is committed (ADR 4): every bridge here is made up."""

import hashlib
import json
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pytest
from psycopg import AsyncConnection
from temporalio.exceptions import ApplicationError

from football_intel.identity.activities import _reep_bridges  # pyright: ignore[reportPrivateUsage]
from football_intel.identity.rules import Method, Proposal, ReepBridge, reep_proposals
from football_intel.identity.store import IdentityStore
from football_intel.reep import load, release

RELEASE = "2026-01-02.1"
PREVIOUS = "2026-01-01.1"

# Person A: Sofascore 1, Transfermarkt 10, Understat 100. Person B was merged into A (a
# redirect), and still carries A's second Transfermarkt bridge. Person C has Transfermarkt
# and Understat bridges but no Sofascore one. Person D has two Transfermarkt bridges: one
# provider, so it is dropped. Transfermarkt's `player` namespace is not ours.
# Team T: Sofascore 42, Transfermarkt 11 (`verein`), and Understat 83 through U, merged into
# T. Transfermarkt's `team` namespace is another ID space: its 42 never loads. Team V has only
# a Sofascore bridge (and a `team`-namespace one), so it is dropped. A match bridge is noise.
BRIDGES = [
    ("rA", "sofascore", "player", "1", "player"),
    ("rA", "transfermarkt", "spieler", "10", "player"),
    ("rB", "understat", "player", "100", "player"),
    ("rB", "transfermarkt", "spieler", "12", "player"),
    ("rC", "transfermarkt", "spieler", "30", "player"),
    ("rC", "understat", "player", "300", "player"),
    ("rD", "transfermarkt", "spieler", "40", "player"),
    ("rD", "transfermarkt", "spieler", "41", "player"),
    ("rD", "transfermarkt", "player", "42", "player"),
    ("rT", "sofascore", "team", "42", "team"),
    ("rT", "transfermarkt", "verein", "11", "team"),
    ("rT", "transfermarkt", "team", "42", "team"),
    ("rU", "understat", "team", "83", "team"),
    ("rV", "sofascore", "team", "83", "team"),
    ("rV", "transfermarkt", "team", "7", "team"),
    ("rT", "sofascore", "event", "5", "match"),
]
REDIRECTS = [("rB", "rA"), ("rU", "rT")]


def _write_release(directory: Path) -> dict[str, bytes]:
    directory.mkdir(parents=True, exist_ok=True)
    with duckdb.connect() as db:
        db.execute(
            "CREATE TABLE b (reep_id text, provider text, namespace text, external_id text,"
            " entity_type text, rule_id text, route_scope text)"
        )
        db.executemany("INSERT INTO b VALUES (?, ?, ?, ?, ?, 'rule', 'scope')", BRIDGES)
        db.execute("CREATE TABLE r (from_id text, to_id text)")
        db.executemany("INSERT INTO r VALUES (?, ?)", REDIRECTS)
        db.execute(f"COPY b TO '{directory / 'bridges.parquet'}' (FORMAT parquet)")
        db.execute(f"COPY r TO '{directory / 'redirects.parquet'}' (FORMAT parquet)")
    return {name: (directory / name).read_bytes() for name in release.FILES}


def _manifest(files: dict[str, bytes], previous: str | None = PREVIOUS) -> dict[str, Any]:
    return {
        "release_tag": RELEASE,
        "checksums_sha256": {
            name: hashlib.sha256(body).hexdigest() for name, body in files.items()
        },
        "gates": {"gate_passed": False},
        "diff": {
            "previous_label": previous,
            "new_bridges_by_provider": {"transfermarkt": 5},
            "id_retention_pct": 100.0,
        },
    }


OBJECTS = "objects/0b7e1c2a-0000-4000-8000-000000000000"


def _release_json(release_tag: str, manifest: bytes, files: dict[str, bytes]) -> dict[str, Any]:
    """A `release.json` (reep-public-release/v4) listing the manifest and files by key."""
    listed = {"manifest.json": manifest, **files}
    return {
        "schema": "reep-public-release/v4",
        "stamp": release_tag,
        "files": {
            name: {
                "key": f"releases/{release_tag}/{OBJECTS}/{name}",
                "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
            }
            for name, body in listed.items()
        },
    }


class ReepStub:
    """Serves releases like data.reep.football, and records every request.

    Each release is its `release.json` and the objects it keys. Flat paths
    (`releases/<stamp>/manifest.json`) answer 404, as Reep's do.
    """

    def __init__(self, releases: dict[str, tuple[dict[str, Any], dict[str, bytes]]], latest: str):
        self.latest = latest
        self.release_jsons: dict[str, dict[str, Any]] = {}
        self.objects: dict[str, bytes] = {}
        for stamp, (manifest, files) in releases.items():
            self.publish(stamp, manifest, files)
        self.requests: list[str] = []

    def publish(
        self,
        stamp: str,
        manifest: dict[str, Any],
        files: dict[str, bytes],
        listed: dict[str, bytes] | None = None,
    ) -> None:
        """Serve `files`, listed in `release.json` with the checksums of `listed` (or `files`)."""
        body = json.dumps(manifest).encode()
        self.release_jsons[stamp] = _release_json(stamp, body, listed or files)
        prefix = f"/releases/{stamp}/{OBJECTS}"
        self.objects[f"{prefix}/manifest.json"] = body
        for name, content in files.items():
            self.objects[f"{prefix}/{name}"] = content

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(path)
        if path == "/releases/latest.json":
            return httpx.Response(200, json={"stamp": self.latest})
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[2] == "release.json" and parts[1] in self.release_jsons:
            return httpx.Response(200, json=self.release_jsons[parts[1]])
        if path in self.objects:
            return httpx.Response(200, content=self.objects[path])
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return release.client(httpx.MockTransport(self.handle))


def _object(stamp: str, name: str) -> str:
    return f"/releases/{stamp}/{OBJECTS}/{name}"


async def test_a_release_downloads_once_verified_from_its_objects(tmp_path: Path) -> None:
    files = _write_release(tmp_path / "source")
    stub = ReepStub({RELEASE: (_manifest(files), files)}, RELEASE)
    target = tmp_path / RELEASE

    async with stub.client() as api:
        manifest = await release.download(api, RELEASE, target)
        await release.download(api, RELEASE, target)

    assert manifest.gate_passed is False
    # The manifest is kept as Reep published it, and the files in our flat local layout.
    assert (target / "manifest.json").read_bytes() == stub.objects[
        _object(RELEASE, "manifest.json")
    ]
    assert {name: (target / name).read_bytes() for name in release.FILES} == files
    # The second run finds both files verified and fetches only release.json and the manifest.
    assert stub.requests.count(_object(RELEASE, "bridges.parquet")) == 1
    assert stub.requests.count(_object(RELEASE, "redirects.parquet")) == 1
    assert not any(path.startswith(f"/releases/{RELEASE}/bridges") for path in stub.requests)


async def test_a_checksum_mismatch_stops_the_download(tmp_path: Path) -> None:
    files = _write_release(tmp_path / "source")
    corrupt = {**files, "bridges.parquet": b"not the published file"}
    stub = ReepStub({}, RELEASE)
    stub.publish(RELEASE, _manifest(files), corrupt, listed=files)

    with pytest.raises(release.ChecksumMismatchError):
        async with stub.client() as api:
            await release.download(api, RELEASE, tmp_path / RELEASE)

    assert not (tmp_path / RELEASE / "bridges.parquet").exists()
    assert not (tmp_path / RELEASE / "bridges.parquet.partial").exists()


async def test_a_release_json_checksum_mismatch_stops_the_download_with_no_file_behind(
    tmp_path: Path,
) -> None:
    # The manifest and the served files agree; release.json says otherwise.
    files = _write_release(tmp_path / "source")
    stub = ReepStub({}, RELEASE)
    stub.publish(RELEASE, _manifest(files), files, listed={**files, "bridges.parquet": b"other"})

    with pytest.raises(release.ChecksumMismatchError):
        async with stub.client() as api:
            await release.download(api, RELEASE, tmp_path / RELEASE)

    assert not (tmp_path / RELEASE).exists()
    assert not any(path.endswith(".parquet") for path in stub.requests)


async def test_a_manifest_not_matching_release_json_stops_the_download(tmp_path: Path) -> None:
    files = _write_release(tmp_path / "source")
    stub = ReepStub({RELEASE: (_manifest(files), files)}, RELEASE)
    stub.objects[_object(RELEASE, "manifest.json")] = b"{}"

    with pytest.raises(release.ChecksumMismatchError):
        async with stub.client() as api:
            await release.download(api, RELEASE, tmp_path / RELEASE)

    assert not (tmp_path / RELEASE).exists()


async def test_a_file_keyed_outside_its_release_is_refused(tmp_path: Path) -> None:
    files = _write_release(tmp_path / "source")
    stub = ReepStub({RELEASE: (_manifest(files), files)}, RELEASE)
    stub.release_jsons[RELEASE]["files"]["bridges.parquet"]["key"] = "elsewhere/bridges.parquet"

    with pytest.raises(release.ReleaseLayoutError):
        async with stub.client() as api:
            await release.download(api, RELEASE, tmp_path / RELEASE)

    assert "/elsewhere/bridges.parquet" not in stub.requests


async def test_status_walks_back_from_the_current_release_to_the_pinned_one(
    tmp_path: Path,
) -> None:
    files = _write_release(tmp_path / "source")
    newest = {**_manifest(files, previous=RELEASE), "release_tag": "2026-01-03.1"}
    stub = ReepStub(
        {"2026-01-03.1": (newest, files), RELEASE: (_manifest(files), files)}, "2026-01-03.1"
    )

    async with stub.client() as api:
        newer = await release.releases_since(api, PREVIOUS)
        none_newer = await release.releases_since(api, "2026-01-03.1")

    assert [m.release for m in newer] == ["2026-01-03.1", RELEASE]
    assert newer[0].new_bridges == {"transfermarkt": 5}
    assert none_newer == []
    # Each manifest is read through its release.json; no bridges are downloaded.
    assert _object(RELEASE, "manifest.json") in stub.requests
    assert not any(path.endswith(".parquet") for path in stub.requests)


def _keys(bridges: list[load.Bridge]) -> list[tuple[str, str, str]]:
    return [(reep_id, provider, external_id) for reep_id, provider, external_id, *_ in bridges]


def test_persons_bridged_to_two_providers_load_with_redirects_resolved(tmp_path: Path) -> None:
    _write_release(tmp_path)

    assert _keys(load.bridges(tmp_path, load.PERSONS)) == [
        ("rA", "sofascore", "1"),
        ("rA", "transfermarkt", "10"),
        ("rA", "transfermarkt", "12"),
        ("rA", "understat", "100"),
        ("rC", "transfermarkt", "30"),
        ("rC", "understat", "300"),
    ]


def test_teams_bridged_to_two_providers_load_without_transfermarkt_team_ids(
    tmp_path: Path,
) -> None:
    _write_release(tmp_path)

    assert _keys(load.bridges(tmp_path, load.TEAMS)) == [
        ("rT", "sofascore", "42"),
        ("rT", "transfermarkt", "11"),
        ("rT", "understat", "83"),
    ]


@pytest.fixture
async def reference(connection: AsyncConnection[Any]) -> AsyncConnection[Any]:
    await connection.execute("TRUNCATE reference.reep_releases CASCADE")
    await connection.execute(
        "TRUNCATE identity.identity_candidates, identity.players RESTART IDENTITY"
    )
    return connection


def _loaded(directory: Path) -> dict[load.Entity, list[load.Bridge]]:
    return {entity: load.bridges(directory, entity) for entity in load.ENTITIES}


async def test_reloading_a_release_replaces_its_rows(
    reference: AsyncConnection[Any], tmp_path: Path
) -> None:
    files = _write_release(tmp_path)
    manifest = release.Manifest(RELEASE, _manifest(files))
    loaded = _loaded(tmp_path)

    await load.load(reference, manifest, loaded)
    await load.load(reference, manifest, {entity: rows[:1] for entity, rows in loaded.items()})

    cursor = await reference.execute(
        "SELECT (SELECT count(*) FROM reference.reep_player_bridges),"
        " (SELECT count(*) FROM reference.reep_team_bridges),"
        " (SELECT gate_passed FROM reference.reep_releases)"
    )
    assert await cursor.fetchone() == (1, 1, False)


async def test_bridges_of_a_release_not_loaded_fail_without_retries(
    reference: AsyncConnection[Any],
) -> None:
    with pytest.raises(ApplicationError) as error:
        await _reep_bridges(reference, RELEASE, "transfermarkt")

    assert error.value.non_retryable


async def test_bridges_map_each_source_to_sofascore_and_skip_persons_without_one(
    reference: AsyncConnection[Any], tmp_path: Path
) -> None:
    files = _write_release(tmp_path)
    await load.load(reference, release.Manifest(RELEASE, _manifest(files)), _loaded(tmp_path))

    # Person C (Transfermarkt 30, Understat 300) is loaded, but has no Sofascore player to
    # link to: Identity Resolution ignores it until it targets Players (ADR 34).
    assert await _reep_bridges(reference, RELEASE, "transfermarkt") == [
        ReepBridge(10, 1, "rule", "scope"),
        ReepBridge(12, 1, "rule", "scope"),
    ]
    assert await _reep_bridges(reference, RELEASE, "understat") == [
        ReepBridge(100, 1, "rule", "scope")
    ]


def test_reep_links_only_records_we_hold_to_one_sofascore_player() -> None:
    bridges = [
        ReepBridge(10, 1),
        ReepBridge(11, 2),  # we do not hold Transfermarkt 11
        ReepBridge(12, 99),  # we do not hold Sofascore 99
        ReepBridge(13, 1),  # Reep maps 13 to two of our Sofascore players: rules decide
        ReepBridge(13, 3),
    ]

    proposals = reep_proposals(RELEASE, bridges, held={10, 12, 13}, sofascore={1, 2, 3})

    assert [(p.source_record_id, p.sofascore_player_id) for p in proposals] == [(10, 1)]
    (proposal,) = proposals
    assert (proposal.method, proposal.decided_by, proposal.accepted) == (
        Method.REEP_BRIDGE,
        f"reep:{RELEASE}",
        True,
    )


async def _statuses(connection: AsyncConnection[Any]) -> list[tuple[Any, ...]]:
    cursor = await connection.execute(
        "SELECT source_record_id, player_id, method, status, decided_by"
        " FROM identity.identity_candidates WHERE source = 'transfermarkt' ORDER BY candidate_id"
    )
    return await cursor.fetchall()


async def test_reep_decides_first_and_the_rules_skip_what_it_linked(
    reference: AsyncConnection[Any],
) -> None:
    store = IdentityStore(reference)
    players, _ = await store.anchor_sofascore([1, 2])

    await store.propose(
        "transfermarkt", reep_proposals(RELEASE, [ReepBridge(10, 1)], {10}, {1, 2}), players
    )
    stored = await store.propose(
        "transfermarkt",
        [
            Proposal(10, 2, Method.NAME_BIRTH_DATE, 1.0),
            Proposal(11, 2, Method.NAME_BIRTH_DATE, 1.0),
        ],
        players,
    )

    assert stored == 1
    assert await _statuses(reference) == [
        ("10", 1, "reep_bridge", "accepted", f"reep:{RELEASE}"),
        ("11", 2, "name_birth_date", "accepted", "rule:name_birth_date"),
    ]


async def test_a_rejected_reep_link_reopens_its_record_to_the_rules(
    reference: AsyncConnection[Any],
) -> None:
    store = IdentityStore(reference)
    players, _ = await store.anchor_sofascore([1, 2])
    await store.propose(
        "transfermarkt", reep_proposals(RELEASE, [ReepBridge(10, 1)], {10}, {1, 2}), players
    )
    await reference.execute(
        "UPDATE identity.identity_candidates SET status = 'rejected' WHERE method = 'reep_bridge'"
    )

    # The same wrong link again is not stored; the rules' own link is.
    stored = await store.propose(
        "transfermarkt",
        [Proposal(10, 1, Method.FUZZY, 0.95), Proposal(10, 2, Method.NAME_BIRTH_DATE, 1.0)],
        players,
    )

    assert stored == 1
    assert await _statuses(reference) == [
        ("10", 1, "reep_bridge", "rejected", f"reep:{RELEASE}"),
        ("10", 2, "name_birth_date", "accepted", "rule:name_birth_date"),
    ]


async def test_a_newer_release_changes_no_existing_candidate(
    reference: AsyncConnection[Any],
) -> None:
    store = IdentityStore(reference)
    players, _ = await store.anchor_sofascore([1, 2])
    await store.propose(
        "transfermarkt", reep_proposals(RELEASE, [ReepBridge(10, 1)], {10}, {1, 2}), players
    )
    before = await _statuses(reference)

    # The newer release maps 10 elsewhere; the existing link stays.
    stored = await store.propose(
        "transfermarkt", reep_proposals("2026-01-03.1", [ReepBridge(10, 2)], {10}, {1, 2}), players
    )

    assert stored == 0
    assert await _statuses(reference) == before
