-- Canonical Players (ADR 34) with their attributes: Transfermarkt's where linked, else
-- Sofascore's (Understat has no birth dates). A Player linked to no Source Record yet is
-- left out. `understat_position` is the Player's usual position in the Understat
-- Appearances they started in the covered Seasons, the detailed one (DL, AMC…) for Players
-- with no Transfermarkt record.
with links as (
    select * from {{ ref('int_player_links') }}
),

transfermarkt as (
    select
        links.player_id,
        players.player_id as transfermarkt_player_id,
        players.name,
        players.birth_date,
        players.nationality,
        players.height_cm,
        players.position,
        players.position_group,
        players.preferred_foot,
        players.current_club_id,
        players.market_value_eur,
        players.market_value_determined_on
    from {{ ref('stg_transfermarkt__players') }} as players
    inner join links
        on links.source = 'transfermarkt' and links.source_record_id = players.player_id
),

sofascore as (
    select
        links.player_id,
        players.player_id as sofascore_player_id,
        players.name,
        players.birth_date,
        players.nationality,
        players.height_cm,
        players.position
    from {{ ref('stg_sofascore__players') }} as players
    inner join links
        on links.source = 'sofascore' and links.source_record_id = players.player_id
),

understat as (
    select links.player_id, players.player_id as understat_player_id, players.name
    from {{ ref('stg_understat__players') }} as players
    inner join links
        on links.source = 'understat' and links.source_record_id = players.player_id
),

understat_positions as (
    select distinct on (links.player_id)
        links.player_id,
        appearances.position
    from {{ ref('stg_understat__appearances') }} as appearances
    inner join {{ ref('int_covered_matches') }} as matches
        on matches.understat_match_id = appearances.match_id
    inner join links
        on links.source = 'understat' and links.source_record_id = appearances.player_id
    where appearances.position <> 'Sub'
    group by links.player_id, appearances.position
    order by links.player_id, count(*) desc, appearances.position
),

transfermarkt_clubs as (
    select external_id::bigint as transfermarkt_club_id, club_id
    from {{ source('identity', 'club_links') }}
    where provider = 'transfermarkt'
)

select
    players.player_id,
    coalesce(transfermarkt.name, sofascore.name, understat.name) as name,
    coalesce(transfermarkt.birth_date, sofascore.birth_date) as birth_date,
    coalesce(transfermarkt.nationality, sofascore.nationality) as nationality,
    coalesce(transfermarkt.height_cm, sofascore.height_cm) as height_cm,
    transfermarkt.position,
    transfermarkt.position_group,
    sofascore.position as sofascore_position,
    understat_positions.position as understat_position,
    transfermarkt.preferred_foot,
    transfermarkt_clubs.club_id as current_club_id,
    transfermarkt.market_value_eur,
    transfermarkt.market_value_determined_on,
    sofascore.sofascore_player_id,
    understat.understat_player_id,
    transfermarkt.transfermarkt_player_id
from {{ source('identity', 'players') }} as players
left join transfermarkt on transfermarkt.player_id = players.player_id
left join sofascore on sofascore.player_id = players.player_id
left join understat on understat.player_id = players.player_id
left join understat_positions on understat_positions.player_id = players.player_id
left join transfermarkt_clubs
    on transfermarkt_clubs.transfermarkt_club_id = transfermarkt.current_club_id
where exists (select from links where links.player_id = players.player_id)
