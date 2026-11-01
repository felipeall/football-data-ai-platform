-- Canonical Clubs (ADR 34), with each provider's ID from their links.
select
    clubs.club_id,
    clubs.name,
    clubs.display_name,
    clubs.country,
    min(links.external_id) filter (where links.provider = 'sofascore')::bigint
        as sofascore_team_id,
    min(links.external_id) filter (where links.provider = 'understat')::bigint
        as understat_team_id,
    min(links.external_id) filter (where links.provider = 'transfermarkt')::bigint
        as transfermarkt_club_id,
    min(links.external_id) filter (where links.provider = 'reep') as reep_team_id
from {{ source('identity', 'clubs') }} as clubs
left join {{ source('identity', 'club_links') }} as links using (club_id)
group by clubs.club_id, clubs.name, clubs.display_name, clubs.country
