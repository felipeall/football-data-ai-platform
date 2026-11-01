# 39. Reep and Transfermarkt create a Player only when no existing Player matches

Status: Accepted — 2026-11-01. Amends ADR 34 (who creates a Player) and ADR 32 (which rejections reopen a Source Record).

## Context
ADR 34 has Reep create a Player for each person it maps, then Transfermarkt create one for each player Reep did not link. Sofascore then links by name and birth date against a Player's Transfermarkt record. A record that already has a Candidate is skipped by every later step and every later run (ADR 32). That leaves two ways for one person to become two Players (#87):

- **A.** Reep creates Player A from a person's Sofascore record but has no bridge for their Transfermarkt record. Transfermarkt then creates Player B from it.
- **B.** Transfermarkt creates Player B before Sofascore has the player, a debutant seen by one source before the other (ingest lag, ADR 34). On a later run a new Reep release bridges the Sofascore record without the Transfermarkt one, and Reep creates Player A. The Sofascore rule never sees the record, since it now has a Candidate.

Merging the two later changes a Player ID, which ADR 34 forbids once IDs are cited. On live data (2026-11-01, Reep `2026-10-31.1`) the count is 0, but a new Reep release or a squad refresh can produce either case.

## Decision
- **The creation check.** Before Reep or Transfermarkt creates a Player, the record about to create it runs the name and birth date rule of the Sofascore step. The rule uses the same `BIRTH_DATE_TOLERANCE_DAYS` window and the same ambiguity rule, against every Player that holds the other source's record and none of this one's:
  - a Transfermarkt record, against Players with a Sofascore link and no Transfermarkt link;
  - a Sofascore record of a Reep person, against Players with a Transfermarkt link and no Sofascore link.

  Understat records have no birth date and are not checked.
- **The same name, and no second same-named Player in the window: link to the existing Player.** The Candidate's method is `creation_check`, and no Player is created. For a Reep person, its other records follow that link to the Player through their `reep_bridge` Candidates.
- **A similar name, or two same-named Players: create nothing.** A pending `creation_check` Candidate points at the existing Player, for the judge or a person. A Reep person waits as a whole while one of its records' checks is pending, so none of its records creates the Player meanwhile.
- **A rejected `creation_check` reopens its record.** On the next run the check skips every Player already rejected for that record. With none left, the record creates its own Player, by Reep or by Transfermarkt. This extends ADR 32's single exception, rejected `reep_bridge` Candidates, to a second method.
- **ADR 34 now reads:** Reep and Transfermarkt create a Player only when no existing Player matches by name and birth date.

## Consequences
- **Scenarios A and B each give one Player.** So does any order in which the two records arrive.
- **A held-back Sofascore record costs minutes.** In the Reep step the record that waits is a Sofascore record, with Appearances. They stay out of the Player marts until the Candidate is decided, and the run lists the record as unlinked with a pending Candidate. A held-back Transfermarkt record costs nothing, since it has no Appearances.
- **Not caught:**
  - Two Reep persons new in the same run that are one human. Reep computes its proposals before storing any.
  - Reep's own errors in general.

  Both still need a Player merge that keeps the old ID resolving, which is left for when the first real duplicate appears (#87).
- **Rejected:**
  - **Detect only, and merge later.** Merging changes an ID.
  - **A check only before Transfermarkt creates.** It misses scenario B.
