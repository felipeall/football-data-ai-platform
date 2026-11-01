-- Appearances and minutes per Player and Season, in the covered Seasons.
select
    player_id || ':' || season as player_season_id,
    player_id,
    season,
    count(*) as appearances,
    sum(minutes_played) as minutes_played
from {{ ref('appearances') }}
group by player_id, season
