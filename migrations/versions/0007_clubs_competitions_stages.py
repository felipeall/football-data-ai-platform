"""Clubs, Competitions and Stages with our own IDs, and the Premier League (ADR 34).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-01
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A Club's ID is a slug from its Transfermarkt name. A rename changes it, and the former
    # slug moves to `club_aliases`, so links and aliases follow the Club on update.
    op.execute(
        """
        CREATE TABLE identity.clubs (
            club_id      text        PRIMARY KEY,
            name         text        NOT NULL,  -- the name the slug was derived from
            display_name text        NOT NULL,
            country      text,
            created_at   timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE identity.club_aliases (
            alias      text        PRIMARY KEY,  -- a former slug of the Club
            club_id    text        NOT NULL REFERENCES identity.clubs ON UPDATE CASCADE,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # Keyed by provider and ID: bare numbers repeat across providers (Sofascore 83 is not
    # Understat 83). `reep` holds the Reep team that created or linked the Club.
    op.execute(
        """
        CREATE TABLE identity.club_links (
            provider    text        NOT NULL CHECK (
                            provider IN ('reep', 'sofascore', 'transfermarkt', 'understat')
                        ),
            external_id text        NOT NULL,
            club_id     text        NOT NULL REFERENCES identity.clubs ON UPDATE CASCADE,
            method      text        NOT NULL,  -- what made the link: the Reep release
            created_at  timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (provider, external_id)
        )
        """
    )
    op.execute("CREATE INDEX club_links_club_idx ON identity.club_links (club_id)")
    op.execute(
        """
        CREATE TABLE identity.competitions (
            competition_id text PRIMARY KEY,  -- a slug we choose, e.g. premier-league
            name           text NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE identity.competition_links (
            provider       text NOT NULL CHECK (
                               provider IN ('sofascore', 'transfermarkt', 'understat')
                           ),
            external_id    text NOT NULL,
            competition_id text NOT NULL REFERENCES identity.competitions,
            PRIMARY KEY (provider, external_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE identity.stages (
            competition_id text    NOT NULL REFERENCES identity.competitions,
            stage_id       text    NOT NULL,  -- a slug we choose, e.g. league, round-of-16
            name           text    NOT NULL,
            is_default     boolean NOT NULL DEFAULT false,
            PRIMARY KEY (competition_id, stage_id)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX stages_one_default_per_competition
        ON identity.stages (competition_id) WHERE is_default
        """
    )
    # A range of a provider's round numbers that belongs to one Stage. A Match whose round
    # no link covers takes its Competition's default Stage.
    op.execute(
        """
        CREATE TABLE identity.stage_links (
            competition_id text NOT NULL,
            stage_id       text NOT NULL,
            provider       text NOT NULL CHECK (provider IN ('sofascore')),
            first_round    int  NOT NULL,
            last_round     int  NOT NULL,
            PRIMARY KEY (competition_id, provider, first_round),
            FOREIGN KEY (competition_id, stage_id) REFERENCES identity.stages,
            CHECK (first_round <= last_round)
        )
        """
    )

    # The Competitions we cover, entered by hand (ADR 34, ADR 35).
    op.execute(
        "INSERT INTO identity.competitions (competition_id, name)"
        " VALUES ('premier-league', 'Premier League')"
    )
    op.execute(
        """
        INSERT INTO identity.competition_links (provider, external_id, competition_id) VALUES
            ('sofascore', '17', 'premier-league'),
            ('understat', 'EPL', 'premier-league'),
            ('transfermarkt', 'GB1', 'premier-league')
        """
    )
    op.execute(
        "INSERT INTO identity.stages (competition_id, stage_id, name, is_default)"
        " VALUES ('premier-league', 'league', 'League', true)"
    )


def downgrade() -> None:
    op.execute(
        "DROP TABLE identity.stage_links, identity.stages, identity.competition_links,"
        " identity.competitions, identity.club_links, identity.club_aliases, identity.clubs"
    )
