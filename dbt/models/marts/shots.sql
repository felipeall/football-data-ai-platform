-- Shots from Sofascore and Understat, in the covered Seasons, keyed to the Player and the
-- canonical Match. Each source's Shots are kept, not merged: one row per source Shot.
-- Coordinates are put in the frame Actions use: 0-100, x from the shooting team's own goal
-- line towards the goal attacked, y from the right touchline to the left one. Own goals are
-- left out, since they are not the player's Shots, as are Shots by an unlinked player.
with links as (
    select * from {{ ref('int_player_links') }}
),

matches as (
    select * from {{ ref('int_covered_matches') }}
),

sofascore as (
    select
        'sofascore:' || shots.shot_id as shot_id,
        'sofascore' as source,
        matches.match_id,
        links.player_id,
        case shots.side when 'home' then matches.home_club_id else matches.away_club_id end
            as club_id,
        matches.season,
        shots.minute,
        shots.added_time,
        case shots.result
            when 'goal' then 'goal'
            when 'save' then 'saved'
            when 'miss' then 'missed'
            when 'post' then 'post'
            when 'block' then 'blocked'
            when 'blocked-off-line' then 'blocked'
        end as result,
        case
            when shots.situation in ('assisted', 'regular', 'fast-break') then 'open-play'
            when shots.situation = 'corner' then 'corner'
            when shots.situation in ('set-piece', 'throw-in-set-piece') then 'set-piece'
            when shots.situation = 'free-kick' then 'direct-free-kick'
            when shots.situation = 'penalty' then 'penalty'
        end as situation,
        shots.body_part,
        -- Sofascore measures x from the goal attacked, and y from the left touchline.
        100 - shots.x as x,
        100 - shots.y as y,
        shots.xg,
        shots.xgot,
        null::bigint as assisted_by_player_id
    from {{ ref('stg_sofascore__shots') }} as shots
    inner join matches on matches.sofascore_match_id = shots.match_id
    inner join links on links.source = 'sofascore' and links.source_record_id = shots.player_id
    where shots.goal_type is distinct from 'own'
),

understat as (
    select
        'understat:' || shots.shot_id as shot_id,
        'understat' as source,
        matches.match_id,
        links.player_id,
        case shots.side when 'home' then matches.home_club_id else matches.away_club_id end
            as club_id,
        matches.season,
        shots.minute,
        null::int as added_time,
        case shots.result
            when 'Goal' then 'goal'
            when 'SavedShot' then 'saved'
            when 'MissedShots' then 'missed'
            when 'ShotOnPost' then 'post'
            when 'BlockedShot' then 'blocked'
        end as result,
        case shots.situation
            when 'OpenPlay' then 'open-play'
            when 'FromCorner' then 'corner'
            when 'SetPiece' then 'set-piece'
            when 'DirectFreekick' then 'direct-free-kick'
            when 'Penalty' then 'penalty'
        end as situation,
        case shots.shot_type
            when 'RightFoot' then 'right-foot'
            when 'LeftFoot' then 'left-foot'
            when 'Head' then 'head'
            when 'OtherBodyPart' then 'other'
        end as body_part,
        shots.x * 100 as x,
        shots.y * 100 as y,
        shots.xg,
        null::numeric as xgot,
        assisters.player_id as assisted_by_player_id
    from {{ ref('stg_understat__shots') }} as shots
    inner join matches on matches.understat_match_id = shots.match_id
    inner join links on links.source = 'understat' and links.source_record_id = shots.player_id
    left join links as assisters
        on assisters.source = 'understat'
        and assisters.source_record_id = shots.assisted_by_player_id
    where shots.result <> 'OwnGoal'
)

select * from sofascore
union all
select * from understat
