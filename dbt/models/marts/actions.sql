-- Located Actions from Sofascore's rating breakdowns, in the covered Seasons, keyed to the
-- Player and the canonical Match. A Source Record linked to no Player stays out.
select
    actions.action_id,
    matches.match_id,
    links.player_id,
    case actions.side
        when 'home' then matches.home_club_id
        else matches.away_club_id
    end as club_id,
    matches.season,
    actions.category,
    actions.action_type,
    actions.outcome,
    actions.x,
    actions.y,
    actions.end_x,
    actions.end_y,
    actions.is_key_pass,
    actions.is_assist,
    actions.is_long_ball
from {{ ref('stg_sofascore__actions') }} as actions
inner join {{ ref('int_covered_matches') }} as matches
    on matches.sofascore_match_id = actions.match_id
inner join {{ ref('int_player_links') }} as links
    on links.source = 'sofascore' and links.source_record_id = actions.player_id
