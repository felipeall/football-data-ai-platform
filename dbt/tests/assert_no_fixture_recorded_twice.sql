-- Two Matches with the same Competition, season, Stage and two Clubs, in either order, that
-- kick off on the same date are one fixture: a neutral-venue match that the sources record
-- with home and away swapped (ADR 34).
select first.match_id, second.match_id as other_match_id
from {{ ref('matches') }} as first
inner join {{ ref('matches') }} as second
    on first.competition_id = second.competition_id
    and first.season = second.season
    and first.stage_id = second.stage_id
    and first.home_club_id = second.away_club_id
    and first.away_club_id = second.home_club_id
    and first.kickoff_at::date = second.kickoff_at::date
    and first.match_id < second.match_id
