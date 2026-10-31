# 32. The Reep register is the first mapping layer of Identity Resolution

Status: Accepted — 2026-10-25. Refines ADR 6 (identity resolution). Extended to Clubs by ADR 33. Amended by ADR 34 (Reep and Transfermarkt create Players; the load keeps entities bridged to two ingested providers).

## Context
Identity Resolution (ADR 6, #47) links Transfermarkt and Understat players to the Sofascore players that anchor Players, with deterministic rules, fuzzy scoring, an LLM judge and human review. Its rules work on the data we have ingested.

[Reep](https://reep.football) is a curated football identity register. It maps players, clubs, matches and competitions across 30+ providers, Sofascore, Transfermarkt and Understat among them. Its releases are CC0: free for any use, attribution optional. A release is a set of Parquet files with a manifest that holds SHA-256 checksums and the release's own quality gates. A bridge maps a provider's ID (`provider`, `namespace`, `external_id`) to a Reep ID. Merged Reep IDs redirect to the survivor. The hosted API is paid beyond a 30-day evaluation; the downloads are not.

A spike on 2026-10-25 compared release `2026-10-25.1` with #47's links (details on #49):

- **Coverage:** 666 of our 1,094 Sofascore players (61 %), and 604 of the 874 who played (69 %). Most of Reep's Sofascore IDs come from Wikidata, which covers established players better than young ones. Coverage grows as we ingest more seasons and leagues.
- **Agreement:** 854 of the 855 links both sides have. The one disagreement is Reep's: it maps Wolves' André to an Understat ID with no Premier League Appearance.
- **Release quality:**
  - this release reports `gate_passed: false`, failing its "detected conflicts" and "lineup coverage" checks;
  - 99.6 % of player bridges have confidence 1.0, so confidence does not separate good bridges from bad ones.

A review of an earlier draft (on #58) proposed using Reep only as evidence for the LLM judge. We chose the simpler, curated-first design below, and accept Reep's rare errors for now.

## Decision
- **Reep decides first.**
  - For every Source Record we hold, Reep's bridge to one of our Sofascore players becomes an accepted Identity Candidate, with method `reep_bridge`, confidence 1.0 and `decided_by = reep:<release tag>`.
  - The bridge's `rule_id` and `route_scope` are kept as evidence.
  - Both IDs must be ones we hold: the Sofascore player in `stg_sofascore__players`, and the Transfermarkt or Understat player in its staging model.
- **Where Reep has no mapping, our rules decide,** as in ADR 6: deterministic rules, fuzzy scoring, the LLM judge and human review. They only see Source Records that Reep did not link. So the order is Reep, then rules, then fuzzy scoring, then the judge, then review.
- **A pinned release, downloaded and loaded by hand.** The steps are under *Release management* below. The worker reads only the loaded table, never the Parquet files.
- **Reep IDs for players we have not ingested are not Candidates.** A link needs a Source Record we hold.
- **Upgrades.**
  - A new release links Source Records that have no Candidate yet.
  - It never changes an existing Candidate: a Source Record is proposed once (#47). Player IDs and accepted links stay stable.
  - **The one exception:** a Source Record whose only Candidates are rejected `reep_bridge` ones is open to our rules again, so rejecting a wrong Reep link lets the rules relink it.
- **Unchanged:**
  - Sofascore anchors Players. *(Replaced by ADR 34: Reep, then Transfermarkt, create Players.)*
  - A Player has at most one link per source.

## Release management
Reep publishes data releases on `data.reep.football`, named by a stamp such as `2026-10-25.1`, often more than one a week. The code tags of the `withqwerty/reep` GitHub repository are not data releases. Nothing pushes a notice of a new release, and no Schedule checks for one (schedules are off while the stack is local). Each step below is a command run by hand.

**Pinning.** `REEP__RELEASE` in `.env` names the release to use, for example `REEP__RELEASE=2026-10-25.1`. The tag is always explicit, never "latest".

**Where a release's files are.** Each release has a file list, `https://data.reep.football/releases/<stamp>/release.json`. Under `files`, it gives every file of the release (the manifest included) a `key`, a `sha256` and a size in `bytes`. The files are served from `https://data.reep.football/<key>`, under `releases/<stamp>/objects/<id>/`. Older releases are served this way too. The flat paths `releases/<stamp>/<file>` answer 404 since October 2026.

**Checking for a new release: `football-intel reep-status`.** It downloads only small JSON files, never the bridges:

1. `https://data.reep.football/releases/latest.json`: its `stamp` is the current release.
2. The manifest of each release from the pinned one up to the current one, found through that release's file list (above). Each manifest's `diff` describes the change from its predecessor: new bridges per provider, entity deltas, ID retention, redirects, and the gate result.

It prints the pinned release, the releases loaded into the database, the current release, and the changes in between for Sofascore, Transfermarkt and Understat. That is enough to decide whether an upgrade is worth the download.

**Downloading and loading: `football-intel load-reep`.** For the pinned release, it:

1. **Reads the file list** at `https://data.reep.football/releases/<tag>/release.json`. A file it does not list, or one keyed outside `releases/<tag>/`, stops the command.
2. **Fetches the manifest** from its key, checks it against the list's SHA-256, and keeps it, byte for byte, as `data/reep/<tag>/manifest.json`.
3. **Downloads** `bridges.parquet` (about 376 MB, every entity type) and `redirects.parquet` (under 1 MB) from their keys into `data/reep/<tag>/`. The local layout does not follow Reep's `objects/` keys.
4. **Verifies each file's SHA-256** against both the file list and the manifest's `checksums_sha256`. A mismatch stops the command and leaves no file behind. Files already present with the right checksum are not downloaded again.
5. **Keeps player bridges** for Sofascore (`player`), Transfermarkt (`spieler`) and Understat (`player`), with each Reep ID resolved through the redirects to its survivor. Only persons that Reep maps to a Sofascore player are kept, about 28k. *(Replaced by ADR 34: person and team bridges of every entity bridged to at least two of the three providers, about 29k persons and 9k teams.)*
6. **Loads them** into the `reference` tables under the tag, replacing that tag's rows if it was loaded before, and records the manifest's gate result and diff with it.

`data/` is outside version control, so the files stay on the machine that ran the command. The hosted stack (M7) runs the same command against its own database.

**Upgrading.**

1. Run `reep-status`.
2. Set `REEP__RELEASE` to the new stamp.
3. Run `load-reep`.
4. Run `resolve-identities`, which uses the pinned release's rows and stops if they are not loaded.

The new release only links Source Records that have no Candidate yet (see *Upgrades*). Older tags' rows can be deleted once no longer pinned. Candidates keep their `decided_by = reep:<tag>`, so their origin stays traceable.

## Proposed, not implemented: our data can veto Reep
Before accepting a Reep bridge, check that our own data does not contradict it:

- **Transfermarkt:** the two birth dates match within one day (sources disagree by a day, as for El Hadji Malick Diouf).
- **Understat:** the two players appeared together in at least one Match for the same Club.

A bridge that fails would become a pending Candidate, listed as a disagreement for the judge or a reviewer, instead of an accepted link. The same check would flag bridges that a new release changes.

This would catch errors like the André bridge in cases where we hold both records. It is left out for simplicity, and can be added without changing this decision's order.

## Consequences
- **Fewer cases for our own matching:** on today's data, Reep links about 855 Source Records, and our rules handle about 550 more where Reep has no mapping.
- **Reep's errors become our errors.** In the spike that was 1 in 855. Such a link is fixed by rejecting its Candidate. Our rules then link the Source Record on the next run.
- **New pinned dependency:** a Parquet reader in the loader (`duckdb` or `pyarrow`), plus about 400 MB per release download under `data/`.
- **Upgrades are manual.** Without a Schedule, a new release is noticed only when someone runs `reep-status`. Once schedules are back, a weekly check could post the diff to an issue.
- **Reep data stays local.** It is CC0, but derived from providers' data, so it stays under `data/` and in the database, and is not redistributed (ADR 4).
- **Out of scope:** Reep's IDs for clubs, competitions and matches. They could replace the hand-made club seed (#46), and need their own decision.
- **Rejected:**
  - **Reep only as evidence for the LLM judge.** Safer, but it leaves the curated mapping unused for the bulk of links.
  - **The hosted API.** It is paid for production use, and adds a network dependency at resolution time.
