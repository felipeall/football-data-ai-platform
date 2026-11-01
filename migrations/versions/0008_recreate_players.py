"""Clear Players and Identity Candidates, so Reep and Transfermarkt recreate them (ADR 34).

Until now every Sofascore player anchored a Player. The next `resolve-identities` creates
Players from the Reep release, then from Transfermarkt. This runs once, and stops, clearing
nothing, if any Candidate was decided by the LLM judge or a reviewer: only the rules and Reep
have decided so far, and ADR 34 relies on that.

Player IDs are not restarted: the new Players take IDs after the old ones, so an old ID
quoted anywhere never names a different player.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-01
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A reviewer may set a status by hand without `decided_by` (the README describes rejecting
    # a Reep link that way), so a rejection, or a decision not made by a rule or Reep, counts.
    op.execute(
        """
        DO $$
        DECLARE
            decided bigint;
        BEGIN
            SELECT count(*) INTO decided FROM identity.identity_candidates
            WHERE status = 'rejected'
                OR (status = 'accepted' AND coalesce(decided_by, '') !~ '^(rule|reep):');
            IF decided > 0 THEN
                RAISE EXCEPTION
                    '% Identity Candidates were decided by the LLM judge or a reviewer; '
                    'recreating Players would lose them (ADR 34)', decided;
            END IF;
        END
        $$
        """
    )
    op.execute("TRUNCATE identity.identity_candidates, identity.players")


def downgrade() -> None:
    # The cleared Candidates are not restored: rerun `resolve-identities` on the old code.
    pass
