-- Each Source Record's Player: the accepted Identity Candidates (ADR 31).
select
    source,
    source_record_id::bigint as source_record_id,
    player_id
from {{ source('identity', 'identity_candidates') }}
where status = 'accepted'
