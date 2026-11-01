{{ config(severity='warn') }}
-- Sofascore and Understat Source Records with Appearances in covered-Season Matches but no
-- Player (ADR 34): their Appearances, Actions and Shots stay out of the Player marts. The
-- warning's count is how many records are left out; each row has their minutes.
with links as (
    select * from {{ ref('int_player_links') }}
),

matches as (
    select * from {{ ref('int_covered_matches') }}
),

appearances as (
    select 'sofascore' as source, appearances.player_id, appearances.player_name,
        appearances.minutes_played
    from {{ ref('stg_sofascore__appearances') }} as appearances
    inner join matches on matches.sofascore_match_id = appearances.match_id
    union all
    select 'understat', appearances.player_id, appearances.player_name,
        appearances.minutes_played
    from {{ ref('stg_understat__appearances') }} as appearances
    inner join matches on matches.understat_match_id = appearances.match_id
)

select
    appearances.source,
    appearances.player_id as source_record_id,
    min(appearances.player_name) as player_name,
    count(*) as appearances,
    sum(appearances.minutes_played) as minutes_played
from appearances
left join links
    on links.source = appearances.source and links.source_record_id = appearances.player_id
where links.player_id is null
group by appearances.source, appearances.player_id
