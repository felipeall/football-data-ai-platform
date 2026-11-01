{# A statistic from a Sofascore lineup entry. Sofascore leaves a zero out, so an Appearance
   Sofascore holds reads 0 for a missing key; one only Understat holds reads null. #}
{% macro sofascore_count(key, alias, type='int') -%}
    case when sofascore.player_id is not null
        then coalesce((sofascore.statistics ->> '{{ key }}')::{{ type }}, 0)
    end as {{ alias }}
{%- endmacro %}
