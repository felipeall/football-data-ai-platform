{{ config(severity='warn') }}
-- Players whose linked Transfermarkt or Sofascore record gives a country that the
-- `country_mappings` seed does not map to a Nationality code. Add a row to the seed. A
-- former country (the USSR, Yugoslavia) gets a code once a Player needs one.
with links as (
    select * from {{ ref('int_player_links') }}
),

source_countries as (
    select
        'transfermarkt' as source,
        players.player_id as source_record_id,
        players.nationality_id::text as source_country,
        players.nationality as source_country_name
    from {{ ref('stg_transfermarkt__players') }} as players
    where players.nationality_id is not null
    union all
    select 'sofascore', players.player_id, players.nationality_alpha3, players.nationality
    from {{ ref('stg_sofascore__players') }} as players
    where players.nationality_alpha3 is not null
)

select
    links.player_id,
    source_countries.source,
    source_countries.source_record_id,
    source_countries.source_country,
    source_countries.source_country_name
from source_countries
inner join links
    on links.source = source_countries.source
    and links.source_record_id = source_countries.source_record_id
left join {{ ref('country_mappings') }} as mappings
    on mappings.source = source_countries.source
    and mappings.source_country = source_countries.source_country
where mappings.country_code is null
