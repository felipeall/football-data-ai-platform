-- Finished Matches. Post-match status checks also store `/event/N`, before kickoff or while
-- the Match is played, so the latest answer decides.
with events as (
    select body_json -> 'event' as event, captured_at
    from {{ ref('base_sofascore__responses') }}
    where url ~ '/api/v1/event/[0-9]+$'
)

select
    (event ->> 'id')::bigint as match_id,
    (event -> 'tournament' -> 'uniqueTournament' ->> 'id')::bigint as tournament_id,
    (event -> 'season' ->> 'id')::bigint as season_id,
    event -> 'season' ->> 'year' as season_year,
    (event -> 'roundInfo' ->> 'round')::int as round,
    to_timestamp((event ->> 'startTimestamp')::bigint) as kickoff_at,
    (event -> 'homeTeam' ->> 'id')::bigint as home_team_id,
    event -> 'homeTeam' ->> 'name' as home_team_name,
    (event -> 'awayTeam' ->> 'id')::bigint as away_team_id,
    event -> 'awayTeam' ->> 'name' as away_team_name,
    (event -> 'homeScore' ->> 'current')::int as home_goals,
    (event -> 'awayScore' ->> 'current')::int as away_goals,
    captured_at
from events
where event -> 'status' ->> 'type' = 'finished'
