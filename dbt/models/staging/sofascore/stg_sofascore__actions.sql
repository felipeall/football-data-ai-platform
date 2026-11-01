-- Located Actions from each player's rating breakdown in a finished Match. A 404 for a
-- rating breakdown means Sofascore has no Actions for that player, so it yields no rows.
-- Coordinates run 0-100: x from the player's own goal line towards the goal attacked, y from
-- the right touchline to the left one. Actions carry no ID, so one is built from the
-- breakdown's category and the Action's place in it.
with breakdowns as (
    select
        substring(url from '/api/v1/event/([0-9]+)/player/')::bigint as match_id,
        substring(url from '/player/([0-9]+)/rating-breakdown$')::bigint as player_id,
        body_json
    from {{ ref('base_sofascore__responses') }}
    where url ~ '/api/v1/event/[0-9]+/player/[0-9]+/rating-breakdown$'
)

select
    breakdowns.match_id || '-' || breakdowns.player_id || '-' || category.key || '-'
    || action.ordinality as action_id,
    breakdowns.match_id,
    breakdowns.player_id,
    case when (action.value ->> 'isHome')::boolean then 'home' else 'away' end as side,
    category.key as category,
    action.value ->> 'eventActionType' as action_type,
    -- Sofascore leaves `outcome` out of a failed Action; a ball carry has none.
    case
        when category.key <> 'ball-carries'
            then coalesce((action.value ->> 'outcome')::boolean, false)
    end as outcome,
    (action.value -> 'playerCoordinates' ->> 'x')::numeric as x,
    (action.value -> 'playerCoordinates' ->> 'y')::numeric as y,
    (action.value -> 'passEndCoordinates' ->> 'x')::numeric as end_x,
    (action.value -> 'passEndCoordinates' ->> 'y')::numeric as end_y,
    coalesce((action.value ->> 'keypass')::boolean, false) as is_key_pass,
    coalesce((action.value ->> 'isAssist')::boolean, false) as is_assist,
    coalesce((action.value ->> 'isLongBall')::boolean, false) as is_long_ball
from breakdowns
inner join {{ ref('stg_sofascore__matches') }} as matches on matches.match_id = breakdowns.match_id
cross join lateral jsonb_each(breakdowns.body_json) as category
cross join lateral jsonb_array_elements(category.value) with ordinality as action (value, ordinality)
where jsonb_typeof(category.value) = 'array'
