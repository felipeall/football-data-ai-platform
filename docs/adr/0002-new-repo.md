# 2. New repository; v1 archived; transfermarkt-api consumed over HTTP

Status: Accepted — 2026-10-11. Repository name superseded by ADR 36.

## Context
`football-data-platform` (v1) is Scrapy-based, Python 3.9, unfinished since 2024.

## Decision
Start a fresh repository, now `football-data-ai-platform` (ADR 36). Link v1 from the README as prior work. Consume `transfermarkt-api` as an external HTTP source rather than vendoring it.

## Consequences
Clean architecture from day one. Dogfooding `transfermarkt-api` shows the author's own API in use.
