-- Transfermarkt players from the batched `/players` responses of the squad fan-out, with
-- their current club and market value as of the latest response.
with players as (
    select player, responses.captured_at
    from {{ ref('base_transfermarkt__responses') }} as responses
    cross join lateral jsonb_array_elements(responses.body_json -> 'data') as player
    where responses.url ~ '^https://tmapi\.transfermarkt\.technology/players\?'
),

latest as (
    select distinct on ((player ->> 'id')::bigint) player
    from players
    order by (player ->> 'id')::bigint, captured_at desc
)

select
    (player ->> 'id')::bigint as player_id,
    player ->> 'name' as name,
    (player -> 'lifeDates' ->> 'dateOfBirth')::date as birth_date,
    -- Transfermarkt's 0 is no nationality.
    nullif((player -> 'nationalityDetails' -> 'nationalities' ->> 'nationalityId')::int, 0)
        as nationality_id,
    countries.name as nationality,
    round((player -> 'attributes' ->> 'height')::numeric * 100)::int as height_cm,
    player -> 'attributes' -> 'position' ->> 'name' as position,
    player -> 'attributes' ->> 'positionGroup' as position_group,
    player -> 'attributes' -> 'preferredFoot' ->> 'name' as preferred_foot,
    (
        select (assignment ->> 'clubId')::bigint
        from jsonb_array_elements(player -> 'clubAssignments') as assignment
        where assignment ->> 'type' = 'current'
        limit 1
    ) as current_club_id,
    -- Transfermarkt shows no value as 0.
    nullif((player -> 'marketValueDetails' -> 'current' ->> 'value')::bigint, 0)
        as market_value_eur,
    case when (player -> 'marketValueDetails' -> 'current' ->> 'value')::bigint > 0
        then (player -> 'marketValueDetails' -> 'current' ->> 'determined')::date
    end as market_value_determined_on
from latest
left join {{ ref('stg_transfermarkt__countries') }} as countries
    on countries.country_id
    = (player -> 'nationalityDetails' -> 'nationalities' ->> 'nationalityId')::int
