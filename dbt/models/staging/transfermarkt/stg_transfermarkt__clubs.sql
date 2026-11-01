-- Transfermarkt clubs from the batched `/clubs` responses: a competition's clubs, and the
-- clubs in players' transfer histories. The country's name comes from `/attributes`.
with clubs as (
    select club, responses.captured_at
    from {{ ref('base_transfermarkt__responses') }} as responses
    cross join lateral jsonb_array_elements(responses.body_json -> 'data') as club
    where responses.url ~ '^https://tmapi\.transfermarkt\.technology/clubs\?'
),

countries as (
    select distinct on ((country ->> 'id')::int)
        (country ->> 'id')::int as country_id,
        country ->> 'name' as name
    from {{ ref('base_transfermarkt__responses') }} as responses
    cross join lateral jsonb_array_elements(responses.body_json -> 'data' -> 'countries') as country
    where responses.url ~ '^https://tmapi\.transfermarkt\.technology/attributes$'
    order by (country ->> 'id')::int, responses.captured_at desc
)

select distinct on ((clubs.club ->> 'id')::bigint)
    (clubs.club ->> 'id')::bigint as club_id,
    clubs.club ->> 'name' as name,
    coalesce(nullif(clubs.club -> 'baseDetails' ->> 'shortName', ''), clubs.club ->> 'name')
        as display_name,
    (clubs.club -> 'baseDetails' ->> 'countryId')::int as country_id,
    countries.name as country
from clubs
left join countries
    on countries.country_id = (clubs.club -> 'baseDetails' ->> 'countryId')::int
order by (clubs.club ->> 'id')::bigint, clubs.captured_at desc
