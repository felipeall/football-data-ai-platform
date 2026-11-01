"""Workflow and activity inputs and results."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SquadIngestInput:
    """One club's squad for one season; `season_id` is the season's start year, e.g. "2025".

    `refresh` fetches every request from the source, even when a fresh Raw Response is stored.
    """

    club_id: str
    season_id: str
    refresh: bool = False


@dataclass(frozen=True)
class SquadIngestResult:
    club_id: str
    season_id: str
    players: int
    # Requests answered by the source (each stored as a new Raw Response), and requests
    # answered by a fresh stored Raw Response instead.
    raw_responses: int
    reused: int

    @property
    def cache_hit(self) -> bool:
        """True when the run sent no request to the source."""
        return self.raw_responses == 0


@dataclass(frozen=True)
class CompetitionClubsInput:
    competition_id: str
    season_id: str
    refresh: bool = False


@dataclass(frozen=True)
class CompetitionClubsResult:
    competition_id: str
    season_id: str
    club_ids: list[str]
    raw_responses: int
    reused: int


@dataclass(frozen=True)
class CompetitionSquadsInput:
    """Every club's squad in one competition season.

    `max_concurrent_clubs` caps the squads in flight: they share the source's rate limit, so
    more at once only makes each squad's activity take longer.
    """

    competition_id: str
    season_id: str
    refresh: bool = False
    max_concurrent_clubs: int = 3


@dataclass(frozen=True)
class CompetitionSquadsResult:
    competition_id: str
    season_id: str
    clubs: CompetitionClubsResult
    squads: list[SquadIngestResult]
    # Clubs whose squad workflow failed; the other clubs still ran to completion.
    failed_club_ids: list[str]


@dataclass(frozen=True)
class MatchIngestInput:
    """One finished Sofascore Match, by Sofascore event ID.

    `refresh` fetches every request from the source, even when a fresh Raw Response is stored.
    `per_player` adds each appearing player's heatmap and rating breakdown to the Match-level
    data.
    """

    event_id: int
    refresh: bool = False
    per_player: bool = True


@dataclass(frozen=True)
class MatchIngestResult:
    event_id: int
    name: str  # "Home v Away"
    players: int  # players who appeared
    # Requests answered by the source (each stored as a new Raw Response), and requests
    # answered by a fresh stored Raw Response instead.
    raw_responses: int
    reused: int

    @property
    def cache_hit(self) -> bool:
        """True when the run sent no request to the source."""
        return self.raw_responses == 0


@dataclass(frozen=True)
class SeasonsInput:
    """A tournament's latest `count` seasons, or only the one with year `year` (e.g. "24/25")."""

    tournament_id: int
    count: int = 3
    year: str | None = None
    refresh: bool = False


@dataclass(frozen=True)
class SeasonRef:
    id: int
    year: str


@dataclass(frozen=True)
class SeasonMatchesInput:
    tournament_id: int
    season_id: int
    refresh: bool = False


@dataclass(frozen=True)
class SeasonMatchesResult:
    event_ids: list[int]  # finished Matches, newest first
    raw_responses: int
    reused: int


@dataclass(frozen=True)
class CompleteMatchesInput:
    event_ids: list[int]
    per_player: bool = True  # complete means per-player data too


@dataclass(frozen=True)
class SeasonBackfillInput:
    """Every finished Match of one season.

    `max_concurrent_matches` caps the Matches in flight: they share the source's rate limit,
    so more at once only makes each Match's activity take longer.
    """

    tournament_id: int
    season: SeasonRef
    refresh: bool = False
    max_concurrent_matches: int = 3
    per_player: bool = True


@dataclass(frozen=True)
class SeasonBackfillResult:
    season: SeasonRef
    matches: int  # finished Matches in the season
    already_complete: int  # skipped: every Raw Response a replay needs was stored
    ingested: int
    # Matches whose ingest failed; the others still ran to completion.
    failed_event_ids: list[int]
    # Over the season's event pages and every ingested Match.
    raw_responses: int
    reused: int
    # Sofascore answered a Match with its bot challenge: the season stopped there, and these
    # Matches were never started. A re-run picks them up.
    challenged: bool = False
    not_attempted_event_ids: list[int] = field(default_factory=list[int])


@dataclass(frozen=True)
class BackfillInput:
    """`per_player` false is the Match-level first pass; true, the full pass that adds
    per-player data."""

    tournament_id: int
    count: int = 3
    year: str | None = None
    refresh: bool = False
    max_concurrent_matches: int = 3
    per_player: bool = True


@dataclass(frozen=True)
class BackfillResult:
    tournament_id: int
    seasons: list[SeasonBackfillResult]
    per_player: bool = True


@dataclass(frozen=True)
class FixturesInput:
    """Start a post-match workflow for each fixture kicking off within `horizon_days`."""

    tournament_id: int
    horizon_days: int = 8


@dataclass(frozen=True)
class FixtureRef:
    event_id: int
    start_timestamp: int  # kickoff, Unix seconds


@dataclass(frozen=True)
class FixturesResult:
    started: list[int]  # Matches whose post-match workflow this run started
    already_scheduled: list[int]  # Matches whose post-match workflow was already running
    beyond_horizon: int  # fixtures left for a later run


@dataclass(frozen=True)
class PostMatchInput:
    event_id: int
    start_timestamp: int  # kickoff when the fixture was read, Unix seconds


@dataclass(frozen=True)
class MatchStatusResult:
    status: str  # Sofascore's status type, e.g. "finished"
    start_timestamp: int


@dataclass(frozen=True)
class PostMatchResult:
    event_id: int
    # "ingested", "postponed", "canceled", or "gave_up" after too many late checks.
    outcome: str
    ingest: MatchIngestResult | None = None


@dataclass(frozen=True)
class UnderstatSeasonInput:
    """One Understat league season; `year` is its start year, as Understat names it (2025 is
    25/26)."""

    league: str
    year: int
    refresh: bool = False


@dataclass(frozen=True)
class UnderstatSeasonResult:
    league: str
    year: int
    matches: int  # played Matches in the league data
    already_complete: int  # stored before, so not requested
    ingested: int
    failed_match_ids: list[int]
    raw_responses: int


@dataclass(frozen=True)
class UnderstatIngestInput:
    """The latest `count` Understat seasons of a league, ending with the current one, or the
    one season `year`."""

    league: str
    count: int = 3
    year: int | None = None
    refresh: bool = False


@dataclass(frozen=True)
class UnderstatIngestResult:
    league: str
    seasons: list[UnderstatSeasonResult]
