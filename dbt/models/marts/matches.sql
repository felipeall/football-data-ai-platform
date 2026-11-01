-- Canonical Matches across Sofascore and Understat (ADR 34). In a league Stage each (home
-- Club, away Club) pair plays once a season, so that key links the sources, whatever their
-- kickoff times say. A Match one source has not stored yet keeps the other's side only.
-- A Match whose Competition or a team has no link is left out; a warning lists it.
with competition_links as (
    select provider, external_id, competition_id
    from {{ source('identity', 'competition_links') }}
),

club_links as (
    select provider, external_id, club_id from {{ source('identity', 'club_links') }}
),

sofascore as (
    select
        competitions.competition_id,
        split_part(matches.season_year, '/', 1)::int + 2000 as season,
        home.club_id as home_club_id,
        away.club_id as away_club_id,
        matches.match_id,
        matches.round,
        matches.kickoff_at,
        matches.home_goals,
        matches.away_goals
    from {{ ref('stg_sofascore__matches') }} as matches
    inner join competition_links as competitions
        on competitions.provider = 'sofascore'
        and competitions.external_id = matches.tournament_id::text
    inner join club_links as home
        on home.provider = 'sofascore' and home.external_id = matches.home_team_id::text
    inner join club_links as away
        on away.provider = 'sofascore' and away.external_id = matches.away_team_id::text
),

understat as (
    select
        competitions.competition_id,
        matches.season,
        home.club_id as home_club_id,
        away.club_id as away_club_id,
        matches.match_id,
        matches.kickoff_at,
        matches.home_goals,
        matches.away_goals
    from {{ ref('stg_understat__matches') }} as matches
    inner join competition_links as competitions
        on competitions.provider = 'understat' and competitions.external_id = matches.league
    inner join club_links as home
        on home.provider = 'understat' and home.external_id = matches.home_team_id::text
    inner join club_links as away
        on away.provider = 'understat' and away.external_id = matches.away_team_id::text
),

-- Paired before the Stage is known: only Sofascore has a round, so a Stage taken per side
-- would split one fixture in two.
paired as (
    select
        coalesce(sofascore.competition_id, understat.competition_id) as competition_id,
        coalesce(sofascore.season, understat.season) as season,
        coalesce(sofascore.home_club_id, understat.home_club_id) as home_club_id,
        coalesce(sofascore.away_club_id, understat.away_club_id) as away_club_id,
        sofascore.round,
        coalesce(sofascore.kickoff_at, understat.kickoff_at) as kickoff_at,
        coalesce(sofascore.home_goals, understat.home_goals) as home_goals,
        coalesce(sofascore.away_goals, understat.away_goals) as away_goals,
        sofascore.match_id as sofascore_match_id,
        understat.match_id as understat_match_id,
        sofascore.home_goals as sofascore_home_goals,
        sofascore.away_goals as sofascore_away_goals,
        understat.home_goals as understat_home_goals,
        understat.away_goals as understat_away_goals
    from sofascore
    full join understat
        on sofascore.competition_id = understat.competition_id
        and sofascore.season = understat.season
        and sofascore.home_club_id = understat.home_club_id
        and sofascore.away_club_id = understat.away_club_id
),

staged as (
    select
        paired.*,
        coalesce(stage_links.stage_id, stages.stage_id) as stage_id
    from paired
    left join {{ source('identity', 'stage_links') }} as stage_links
        on stage_links.competition_id = paired.competition_id
        and stage_links.provider = 'sofascore'
        and paired.round between stage_links.first_round and stage_links.last_round
    left join {{ source('identity', 'stages') }} as stages
        on stages.competition_id = paired.competition_id and stages.is_default
)

select
    competition_id || ':' || season || ':' || stage_id || ':' || home_club_id || ':'
    || away_club_id as match_id,
    competition_id,
    season,
    stage_id,
    round,
    home_club_id,
    away_club_id,
    kickoff_at,
    home_goals,
    away_goals,
    sofascore_match_id,
    understat_match_id,
    sofascore_home_goals,
    sofascore_away_goals,
    understat_home_goals,
    understat_away_goals
from staged
