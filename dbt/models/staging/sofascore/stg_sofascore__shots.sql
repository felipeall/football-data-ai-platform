-- Shots from each finished Match's shotmap, with xG and xGOT. Coordinates are as Sofascore
-- gives them: x is the distance from the goal attacked, and y runs from the left touchline
-- (0) to the right one (100), from the shooter's point of view. `marts.shots` turns them
-- into the frame Actions use.
with shotmaps as (
    select
        substring(url from '/api/v1/event/([0-9]+)/shotmap$')::bigint as match_id,
        body_json
    from {{ ref('base_sofascore__responses') }}
    where url ~ '/api/v1/event/[0-9]+/shotmap$'
)

select
    (shot ->> 'id')::bigint as shot_id,
    shotmaps.match_id,
    (shot -> 'player' ->> 'id')::bigint as player_id,
    shot -> 'player' ->> 'name' as player_name,
    case when (shot ->> 'isHome')::boolean then 'home' else 'away' end as side,
    (shot ->> 'time')::int as minute,
    (shot ->> 'addedTime')::int as added_time,
    shot ->> 'shotType' as result,
    shot ->> 'goalType' as goal_type,
    shot ->> 'situation' as situation,
    shot ->> 'bodyPart' as body_part,
    (shot -> 'playerCoordinates' ->> 'x')::numeric as x,
    (shot -> 'playerCoordinates' ->> 'y')::numeric as y,
    (shot ->> 'xg')::numeric as xg,
    (shot ->> 'xgot')::numeric as xgot
from shotmaps
inner join {{ ref('stg_sofascore__matches') }} as matches on matches.match_id = shotmaps.match_id
cross join lateral jsonb_array_elements(shotmaps.body_json -> 'shotmap') as shot
