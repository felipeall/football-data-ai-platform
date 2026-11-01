{{ config(severity='warn') }}
-- Staged Matches whose Competition or a team has no link, so `marts.matches` leaves them out
-- (ADR 34). `fdap transform` lists why each team has no Club.
with links as (
    select provider, external_id from {{ source('identity', 'club_links') }}
),

competitions as (
    select provider, external_id from {{ source('identity', 'competition_links') }}
)

select 'sofascore' as source, match_id, home_team_name, away_team_name
from {{ ref('stg_sofascore__matches') }}
where (tournament_id::text) not in (
        select external_id from competitions where provider = 'sofascore'
    )
    or (home_team_id::text) not in (select external_id from links where provider = 'sofascore')
    or (away_team_id::text) not in (select external_id from links where provider = 'sofascore')
union all
select 'understat', match_id, home_team_name, away_team_name
from {{ ref('stg_understat__matches') }}
where league not in (select external_id from competitions where provider = 'understat')
    or (home_team_id::text) not in (select external_id from links where provider = 'understat')
    or (away_team_id::text) not in (select external_id from links where provider = 'understat')
