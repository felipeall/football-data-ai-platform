{# The Seasons the Player marts hold, as an SQL list: `(2025)`. #}
{% macro covered_seasons() -%}
    ({{ var('covered_seasons') | join(', ') }})
{%- endmacro %}
