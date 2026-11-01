# 34. Canonical IDs are ours, created from Reep, then Transfermarkt

Status: Accepted — 2026-10-25. Supersedes ADR 33. Amends ADR 31 (Transform Run steps) and ADR 32 (who creates Players, the Reep load). Amended by ADR 35: Transfermarkt squads for 25/26 only; a past squad is the end-of-season roster.

## Context
Every scraped source may go away: a site can close, change its API, or block scraping for good. Sofascore did for six days in October 2026 (ADR 30). So the platform's canonical entities must not take their identity from any scraped source.

Before this decision:

- **Players:** IDs are ours (`identity.players`), but only Sofascore creates them, and every rule matches into a Sofascore player (`rules.py`).
- **Clubs:** ADR 33 made Reep's team ID the Club ID.
- **Matches:** the Match ID is built from the Club IDs.
- **The Reep load:** keeps only persons and teams with a Sofascore bridge.

Two reviews on #66 shaped this decision.

- **Letting any source create entities duplicates them.** Transfermarkt and Understat share no strong signal, Understat has no birth dates, and ingest lag between sources makes a debutant appear in one source before another.
- **Club slugs chosen by a person**, or by dropping suffixes like "United" "unless it clashes", either block new seasons on a manual step or depend on which clubs already exist.
- **Reep's canonical names are unsuitable for slugs.** They are in local languages ("Bayern München"), carry inconsistent suffixes, and at least one is wrong: Everton's entity is named "Everton AFC", Republic of Ireland.
- **Coverage, from a check on 2026-10-25:** Reep covers 604 of the 874 players who played in our stored Matches. Of the other 270, Transfermarkt has 125, because only current squads are ingested.

## Decision
- **Canonical IDs are ours,** kept in the Alembic-managed `identity` schema (ADR 31). A Player or Club ID never changes once created, and is never a source's or Reep's ID. A source's ID is only ever a link to one of ours.
- **Who creates an entity, in order:**
  1. **The Reep register.** A Reep person or team that a stored source record maps to becomes a Player or Club the first time we see it. Every source record mapped to the same Reep entity links to it.
  2. **Transfermarkt.** A Transfermarkt player or club (`verein`) with no Reep entity becomes a Player or Club.

  No scraped match source creates entities: not Sofascore, not Understat. Their records link to existing Players and Clubs, through Reep first, then our rules (deterministic, fuzzy scoring, the LLM judge, review; ADR 6). A Sofascore or Understat record that matches neither stays unlinked, and is listed by each run. Its Appearances stay out of the Player marts until a newer Reep release or a Transfermarkt refresh covers the player.
- **Rules match into Players, not into a source.**
  - **Sofascore and Transfermarkt:** birth date and name, against the Player's Transfermarkt record.
  - **Understat:** co-appearance with the Player's linked Sofascore Appearances, as today.
  - **Reep:** a bridge to any source already linked to the Player.

  `rules.py` is rewritten to target Players.
- **Transfermarkt squads are ingested for every season we cover,** not only the current one. Historical squads list every player registered with a club in that season, so the 145 players who played but are in neither Reep nor the current squads gain a Transfermarkt record. A mid-season signing or debutant is created once the squads are refreshed, by hand while schedules are off.
- **Clubs.**
  - **The ID is a slug derived from one fixed name:** the club's Transfermarkt name, in English. Reep's canonical name is used only when the club has no Transfermarkt bridge. The rule:
    1. transliterate to ASCII and lower-case;
    2. turn `&` into `and`, and drop apostrophes and dots;
    3. drop only the whole tokens `fc`, `afc` and `cf`;
    4. turn each run of other characters into one `-` and trim.

    No other words are dropped. So `arsenal`, `brighton-and-hove-albion`, `west-ham-united`, `wolverhampton-wanderers`, `manchester-united`.
  - **A slug already taken** gets `-<country>` appended, from the Transfermarkt or Reep country. If that is taken too, that one Club waits for a person.
  - **The slug is created automatically,** with no manual step. A person may rename a Club at any time. `identity.club_aliases` keeps every former slug, and the Match IDs built from it, resolving.
  - **Scope:** Clubs are created only for teams that appear in stored Matches of the Competitions we cover. A player's current club in Transfermarkt does not create a Club; it stays an attribute.
- **Competitions and Stages.**
  - `identity.competitions` holds a slug we choose (`premier-league`), with links from each source's competition ID (Sofascore 17, Understat `EPL`, Transfermarkt `GB1`). Both are added by hand when we take on a competition.
  - Each competition has named Stages (`league`; for a cup, `league-phase`, `round-of-16`, `final`) and a default Stage. Sofascore exposes only a round number and Understat nothing, so a stage link can map a range of round numbers to a Stage. Without a link, a Match takes the competition's default Stage.
  - A league where the same home and away pair meets more than once in one phase (the Scottish Premiership) is out of scope until it gets its own rule.
- **Matches.** The ID is `<competition>:<season>:<stage>:<home club>:<away club>`, such as `premier-league:2025:league:arsenal:west-ham-united`.
  - It is derived by dbt from our IDs and links, not stored. It changes only when a Club, Stage or link is deliberately changed, and a Club's aliases keep an old ID resolving.
  - The round is not part of the ID. Only some sources have one (Sofascore's `roundInfo.round`, Transfermarkt's matchday, not Understat), and a postponed fixture can be renumbered. In a league season the pair is already unique. `marts.matches` carries `round` as a column, from whichever source has it.
  - dbt's `unique` test guards the key.
  - A second test fails if two Matches share competition, season, stage, the same two Clubs in either order, and the same kickoff date. That catches a neutral-venue match the sources record with home and away swapped.
- **The Reep load** keeps person and team bridges only, for every provider we ingest. Only Reep entities bridged to at least two of those providers are kept: 28,714 persons and 9,012 teams in release `2026-10-25.1`.
- **The Transform Run (ADR 31) runs in four steps:**
  1. dbt builds staging.
  2. The identity step creates and links Clubs, from Reep, then Transfermarkt, then the stored Matches' teams.
  3. dbt builds Clubs and Matches, the exact models named, not a `+` selection.
  4. Identity Resolution runs for Players, then dbt builds the rest.

  An unlinked team keeps only its own Matches out of the marts, with a warning. It never fails the run.

## Consequences
- **Losing Sofascore or Understat** keeps every ID and stops only that source's links. Losing Reep keeps its last release working, and Transfermarkt creates new entities. Losing Transfermarkt leaves Reep creating new entities, with Reep's coverage gaps.
- **Some players can wait for a Player:** those in neither Reep nor any ingested Transfermarkt squad. They are listed, not given an ID from a match source.
- **Slugs are long but deterministic** (`brighton-and-hove-albion`), the same whatever the processing order. A shorter display name is a separate column.
- **New `identity` tables:** `clubs`, `club_aliases`, `club_links`, `competitions`, `competition_links`, `stages`, `stage_links`. Player Identity Candidates gain Reep and Transfermarkt as creating methods.
- **Today's IDs change once:** the Players are recreated from Reep and Transfermarkt, and the Match IDs gain the competition and stage. No human or judge decision exists yet, and nothing cites these IDs, so nothing is lost.
- **ADR 33 is superseded.** Its decision that the Club ID is Reep's is replaced. Loading team bridges and deleting the club seed are kept.
- **ADR 32 is amended:**
  - Reep and Transfermarkt create Players, and Sofascore no longer anchors them.
  - The Reep load is filtered as above.
- **ADR 31 is amended:** the Transform Run has four steps.
- **Rejected:**
  - **Sofascore as the creating source, or a fallback.** It ties identity to a scraped match source that has already been unreachable once.
  - **Any source creating entities.** It produces duplicates.
  - **Person-chosen or suffix-dropping slugs.** They block on a person, or depend on order.
  - **Reep's or a source's IDs as our IDs.** They tie the platform to one of them.
