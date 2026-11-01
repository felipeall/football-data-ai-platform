-- One row per Player and Match, in the covered Seasons: Sofascore's Appearance and
-- Understat's, joined on the Player. A Source Record linked to no Player stays out, and
-- `warn_appearances_left_out_of_the_player_marts` lists it.
with links as (
    select * from {{ ref('int_player_links') }}
),

matches as (
    select * from {{ ref('int_covered_matches') }}
),

sofascore as (
    select
        matches.match_id,
        links.player_id,
        case appearances.side
            when 'home' then matches.home_club_id
            else matches.away_club_id
        end as club_id,
        appearances.position,
        appearances.substitute,
        appearances.minutes_played,
        appearances.rating,
        appearances.statistics
    from {{ ref('stg_sofascore__appearances') }} as appearances
    inner join matches on matches.sofascore_match_id = appearances.match_id
    inner join links
        on links.source = 'sofascore' and links.source_record_id = appearances.player_id
),

understat as (
    select
        matches.match_id,
        links.player_id,
        case appearances.side
            when 'home' then matches.home_club_id
            else matches.away_club_id
        end as club_id,
        appearances.position,
        appearances.minutes_played,
        appearances.goals,
        appearances.assists,
        appearances.shots,
        appearances.key_passes,
        appearances.xg,
        appearances.xa,
        appearances.xg_chain,
        appearances.xg_buildup
    from {{ ref('stg_understat__appearances') }} as appearances
    inner join matches on matches.understat_match_id = appearances.match_id
    inner join links
        on links.source = 'understat' and links.source_record_id = appearances.player_id
)

select
    coalesce(sofascore.match_id, understat.match_id) || ':'
    || coalesce(sofascore.player_id, understat.player_id) as appearance_id,
    coalesce(sofascore.match_id, understat.match_id) as match_id,
    coalesce(sofascore.player_id, understat.player_id) as player_id,
    coalesce(sofascore.club_id, understat.club_id) as club_id,
    matches.season,
    matches.kickoff_at,
    coalesce(not sofascore.substitute, understat.position <> 'Sub') as started,
    coalesce(sofascore.minutes_played, understat.minutes_played) as minutes_played,
    sofascore.position as sofascore_position,
    understat.position as understat_position,
    sofascore.rating,
    {{ sofascore_count('goals', 'goals') }},
    {{ sofascore_count('goalAssist', 'assists') }},
    {{ sofascore_count('totalShots', 'shots') }},
    {{ sofascore_count('onTargetScoringAttempt', 'shots_on_target') }},
    {{ sofascore_count('expectedGoals', 'expected_goals', 'numeric') }},
    {{ sofascore_count('expectedAssists', 'expected_assists', 'numeric') }},
    {{ sofascore_count('bigChanceCreated', 'big_chances_created') }},
    {{ sofascore_count('bigChanceMissed', 'big_chances_missed') }},
    {{ sofascore_count('keyPass', 'key_passes') }},
    {{ sofascore_count('touches', 'touches') }},
    {{ sofascore_count('totalPass', 'passes') }},
    {{ sofascore_count('accuratePass', 'accurate_passes') }},
    {{ sofascore_count('totalLongBalls', 'long_balls') }},
    {{ sofascore_count('accurateLongBalls', 'accurate_long_balls') }},
    {{ sofascore_count('totalCross', 'crosses') }},
    {{ sofascore_count('accurateCross', 'accurate_crosses') }},
    {{ sofascore_count('totalContest', 'dribbles') }},
    {{ sofascore_count('wonContest', 'successful_dribbles') }},
    {{ sofascore_count('ballCarriesCount', 'ball_carries') }},
    {{ sofascore_count('progressiveBallCarriesCount', 'progressive_ball_carries') }},
    {{ sofascore_count('totalTackle', 'tackles') }},
    {{ sofascore_count('wonTackle', 'tackles_won') }},
    {{ sofascore_count('interceptionWon', 'interceptions') }},
    {{ sofascore_count('totalClearance', 'clearances') }},
    {{ sofascore_count('outfielderBlock', 'blocks') }},
    {{ sofascore_count('ballRecovery', 'ball_recoveries') }},
    {{ sofascore_count('duelWon', 'duels_won') }},
    {{ sofascore_count('duelLost', 'duels_lost') }},
    {{ sofascore_count('aerialWon', 'aerials_won') }},
    {{ sofascore_count('aerialLost', 'aerials_lost') }},
    {{ sofascore_count('possessionLostCtrl', 'possession_lost') }},
    {{ sofascore_count('dispossessed', 'dispossessed') }},
    {{ sofascore_count('fouls', 'fouls') }},
    {{ sofascore_count('wasFouled', 'was_fouled') }},
    {{ sofascore_count('saves', 'saves') }},
    understat.xg as understat_xg,
    understat.xa as understat_xa,
    understat.xg_chain as understat_xg_chain,
    understat.xg_buildup as understat_xg_buildup,
    sofascore.statistics as sofascore_statistics
from sofascore
full join understat
    on understat.match_id = sofascore.match_id and understat.player_id = sofascore.player_id
inner join matches on matches.match_id = coalesce(sofascore.match_id, understat.match_id)
