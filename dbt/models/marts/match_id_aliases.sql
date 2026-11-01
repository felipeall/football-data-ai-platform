-- Former Match IDs: those built from a Club's former slugs (ADR 34). Empty until a rename.
with club_ids as (
    select club_id, club_id as slug from {{ source('identity', 'clubs') }}
    union all
    select club_id, alias from {{ source('identity', 'club_aliases') }}
)

select
    matches.competition_id || ':' || matches.season || ':' || matches.stage_id || ':'
    || home.slug || ':' || away.slug as alias,
    matches.match_id
from {{ ref('matches') }} as matches
inner join club_ids as home on home.club_id = matches.home_club_id
inner join club_ids as away on away.club_id = matches.away_club_id
where home.slug <> matches.home_club_id or away.slug <> matches.away_club_id
