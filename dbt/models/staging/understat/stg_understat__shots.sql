-- Shots with their location and xG. X runs 0-1 from the shooting team's own goal line, and Y
-- from the right touchline to the left one. Understat names the assister only; the player
-- of that name in the shooting team's roster for the Match gives their ID.
with match_data as (
    select
        substring(url from '/getMatchData/([0-9]+)$')::bigint as match_id,
        body_json
    from {{ ref('base_understat__responses') }}
    where url ~ '/getMatchData/[0-9]+$'
)

select
    (shot ->> 'id')::bigint as shot_id,
    match_data.match_id,
    (shot ->> 'player_id')::bigint as player_id,
    {{ html_unescape("shot ->> 'player'") }} as player_name,
    case shot ->> 'h_a' when 'h' then 'home' else 'away' end as side,
    (shot ->> 'minute')::int as minute,
    shot ->> 'result' as result,
    (shot ->> 'X')::numeric as x,
    (shot ->> 'Y')::numeric as y,
    (shot ->> 'xG')::numeric as xg,
    shot ->> 'situation' as situation,
    shot ->> 'shotType' as shot_type,
    shot ->> 'lastAction' as last_action,
    {{ html_unescape("shot ->> 'player_assisted'") }} as assisted_by,
    (
        select case when count(*) = 1 then min((roster.value ->> 'player_id')::bigint) end
        from jsonb_each(match_data.body_json -> 'rosters' -> side.side) as roster
        where roster.value ->> 'player' = shot ->> 'player_assisted'
    ) as assisted_by_player_id
from match_data
cross join lateral (values ('h'), ('a')) as side (side)
cross join lateral jsonb_array_elements(match_data.body_json -> 'shots' -> side.side) as shot
inner join {{ ref('stg_understat__matches') }} as matches on matches.match_id = match_data.match_id
