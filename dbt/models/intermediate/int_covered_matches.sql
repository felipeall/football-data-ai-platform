-- The canonical Matches of the Seasons the Player marts hold (`covered_seasons`).
select *
from {{ ref('matches') }}
where season in {{ covered_seasons() }}
