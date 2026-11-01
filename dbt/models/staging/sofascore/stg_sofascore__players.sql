-- Every player in a stored lineup, played or not, with the latest details Sofascore gave.
with entries as (
    select
        entry -> 'player' as player,
        responses.captured_at
    from {{ ref('base_sofascore__responses') }} as responses
    cross join lateral (values ('home'), ('away')) as side (side)
    cross join lateral jsonb_array_elements(responses.body_json -> side.side -> 'players') as entry
    where responses.url ~ '/api/v1/event/[0-9]+/lineups$'
)

select distinct on ((player ->> 'id')::bigint)
    (player ->> 'id')::bigint as player_id,
    player ->> 'name' as name,
    to_timestamp((player ->> 'dateOfBirthTimestamp')::bigint)::date as birth_date,
    player -> 'country' ->> 'name' as nationality,
    player ->> 'position' as position,
    (player ->> 'height')::int as height_cm
from entries
order by (player ->> 'id')::bigint, captured_at desc
