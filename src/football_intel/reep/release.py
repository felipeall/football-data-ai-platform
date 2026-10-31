"""Reep's published releases: which is current, what changed, and downloading one.

`data.reep.football/releases/latest.json` names the current release. Each release has a
`releases/<stamp>/release.json` that lists its files, each with a key under
`releases/<stamp>/objects/` and a SHA-256. One of them is the manifest, which holds the files'
checksums again, the release's quality gates and a diff from its predecessor.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "https://data.reep.football"
TIMEOUT = httpx.Timeout(60.0, connect=10.0)
FILES = ("bridges.parquet", "redirects.parquet")
MANIFEST = "manifest.json"
# The chain from the current release back to the pinned one is followed at most this far.
MAX_CHAIN = 100


class ChecksumMismatchError(Exception):
    """A downloaded file does not match its checksum in `release.json` or the manifest."""


class ReleaseLayoutError(Exception):
    """A `release.json` lacks a file we need, or keys one outside its release."""


@dataclass(frozen=True)
class Manifest:
    release: str
    data: dict[str, Any]

    @property
    def checksums(self) -> dict[str, str]:
        return self.data["checksums_sha256"]

    @property
    def gate_passed(self) -> bool | None:
        return self.data.get("gates", {}).get("gate_passed")

    @property
    def previous(self) -> str | None:
        return self.data.get("diff", {}).get("previous_label")

    @property
    def new_bridges(self) -> dict[str, int]:
        return self.data.get("diff", {}).get("new_bridges_by_provider", {})


@dataclass(frozen=True)
class ReleaseFile:
    """A file of a release as `release.json` lists it."""

    name: str
    key: str
    sha256: str


def client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=BASE_URL, transport=transport, timeout=TIMEOUT)


async def latest_release(api: httpx.AsyncClient) -> str:
    """The stamp of the release Reep currently publishes."""
    response = await api.get("/releases/latest.json")
    response.raise_for_status()
    return response.json()["stamp"]


async def release_files(api: httpx.AsyncClient, release: str) -> dict[str, ReleaseFile]:
    """The manifest and the files we load, as the release's `release.json` lists them."""
    response = await api.get(f"/releases/{release}/release.json")
    response.raise_for_status()
    listed: dict[str, Any] = response.json().get("files", {})
    files: dict[str, ReleaseFile] = {}
    for name in (MANIFEST, *FILES):
        if name not in listed:
            raise ReleaseLayoutError(f"release.json of {release} lists no {name}")
        key = listed[name]["key"]
        if not key.startswith(f"releases/{release}/") or ".." in key.split("/"):
            raise ReleaseLayoutError(f"{name} of {release} is keyed outside it: {key}")
        files[name] = ReleaseFile(name, key, listed[name]["sha256"])
    return files


async def fetch_manifest(api: httpx.AsyncClient, release: str) -> Manifest:
    manifest, _, _ = await _manifest(api, release)
    return manifest


async def releases_since(api: httpx.AsyncClient, pinned: str) -> list[Manifest]:
    """The manifests of the releases after `pinned`, newest first, up to the current one.

    Each manifest names its predecessor, so the chain is followed back from the current
    release until `pinned`, a gap, or `MAX_CHAIN` releases.
    """
    manifests: list[Manifest] = []
    release: str | None = await latest_release(api)
    while release is not None and release != pinned and len(manifests) < MAX_CHAIN:
        manifest = await fetch_manifest(api, release)
        manifests.append(manifest)
        release = manifest.previous
    return manifests


async def download(api: httpx.AsyncClient, release: str, directory: Path) -> Manifest:
    """Download a release's manifest and files into `directory`, verifying each file.

    Each file must match both its `release.json` checksum and the manifest's. A file already
    there with the right checksum is not downloaded again. A download goes to a temporary
    file, renamed into place only once its checksum matches, so an interrupted or corrupt
    download never looks like a valid file.
    """
    manifest, body, files = await _manifest(api, release)
    for name in FILES:
        if manifest.checksums.get(name) != files[name].sha256:
            raise ChecksumMismatchError(
                f"{name} of {release}: release.json says {files[name].sha256}, "
                f"manifest says {manifest.checksums.get(name)}"
            )
    directory.mkdir(parents=True, exist_ok=True)
    (directory / MANIFEST).write_bytes(body)
    for name in FILES:
        target = directory / name
        expected = files[name].sha256
        if target.exists() and sha256(target) == expected:
            continue
        partial = directory / f"{name}.partial"
        async with api.stream("GET", f"/{files[name].key}") as response:
            response.raise_for_status()
            with partial.open("wb") as file:
                async for chunk in response.aiter_bytes():
                    file.write(chunk)
        actual = sha256(partial)
        if actual != expected:
            partial.unlink()
            raise ChecksumMismatchError(
                f"{name} of {release}: sha256 {actual}, expected {expected}"
            )
        partial.replace(target)
    return manifest


async def _manifest(
    api: httpx.AsyncClient, release: str
) -> tuple[Manifest, bytes, dict[str, ReleaseFile]]:
    """A release's manifest, verified against its `release.json`, with its published bytes."""
    files = await release_files(api, release)
    response = await api.get(f"/{files[MANIFEST].key}")
    response.raise_for_status()
    body = response.content
    actual = hashlib.sha256(body).hexdigest()
    if actual != files[MANIFEST].sha256:
        raise ChecksumMismatchError(
            f"{MANIFEST} of {release}: sha256 {actual}, release.json says {files[MANIFEST].sha256}"
        )
    return Manifest(release, json.loads(body)), body, files


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
