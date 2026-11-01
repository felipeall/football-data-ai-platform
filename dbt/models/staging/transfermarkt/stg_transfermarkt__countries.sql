-- Transfermarkt's countries, by ID, from `/attributes`. Clubs and players name theirs by ID.
select distinct on ((country ->> 'id')::int)
    (country ->> 'id')::int as country_id,
    country ->> 'name' as name
from {{ ref('base_transfermarkt__responses') }} as responses
cross join lateral jsonb_array_elements(responses.body_json -> 'data' -> 'countries') as country
where responses.url ~ '^https://tmapi\.transfermarkt\.technology/attributes$'
order by (country ->> 'id')::int, responses.captured_at desc
