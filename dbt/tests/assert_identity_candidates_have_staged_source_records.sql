-- Every Identity Candidate's Source Record is still in staging. Identity Resolution only
-- proposes records staging holds, so one missing here was lost from `raw` or dropped by a
-- parser, and rebuilding identity from raw data would silently lose its Player's link (#74).
-- An anti-join, not a correlated subquery: the staging views are evaluated once each.
with staged as (
    select 'sofascore' as source, player_id::text as source_record_id
    from {{ ref('stg_sofascore__players') }}
    union all
    select 'transfermarkt', player_id::text from {{ ref('stg_transfermarkt__players') }}
    union all
    select 'understat', player_id::text from {{ ref('stg_understat__players') }}
)

select candidates.candidate_id, candidates.source, candidates.source_record_id,
    candidates.player_id, candidates.method, candidates.status
from {{ source('identity', 'identity_candidates') }} as candidates
left join staged
    on staged.source = candidates.source
    and staged.source_record_id = candidates.source_record_id
where staged.source is null
