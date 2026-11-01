"""Ingestion worker: runs the workflows and activities on the ingestion task queue."""

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from football_data_ai_platform.config import Settings
from football_data_ai_platform.identity.activities import IdentityActivities
from football_data_ai_platform.ingestion.activities import (
    SofascoreActivities,
    TransfermarktActivities,
    UnderstatActivities,
)
from football_data_ai_platform.ingestion.workflows import (
    BackfillWorkflow,
    CompetitionSquadsWorkflow,
    FixturesWorkflow,
    MatchIngestWorkflow,
    PostMatchWorkflow,
    SeasonBackfillWorkflow,
    SquadIngestWorkflow,
    UnderstatIngestWorkflow,
)
from football_data_ai_platform.sources import sofascore, understat
from football_data_ai_platform.sources.impersonation import impersonating_backend
from football_data_ai_platform.sources.interface import SourceInterface
from football_data_ai_platform.sources.sofascore.captcha import (
    CaptchaTransport,
    TokenProvider,
    camoufox_solver,
)
from football_data_ai_platform.sources.transfermarkt import SOURCE, is_upstream_failure
from football_data_ai_platform.transform.dbt import DbtActivities
from football_data_ai_platform.transform.workflows import TransformWorkflow


async def run_worker(settings: Settings) -> None:
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    transfermarkt = SourceInterface(SOURCE, settings.transfermarkt, is_failure=is_upstream_failure)
    # One Token Provider per worker: every Sofascore request shares its Captcha Token.
    captcha = TokenProvider(
        camoufox_solver(settings.sofascore.captcha_headless, settings.sofascore.proxy)
    )
    # One impersonating session too: the token is bound to the client that exchanged it.
    sofascore_session = impersonating_backend(settings.sofascore)
    sofascore_source = SourceInterface(
        sofascore.SOURCE,
        settings.sofascore,
        backend=lambda: CaptchaTransport(sofascore_session, captcha),
    )
    activities = TransfermarktActivities(settings.database_url, transfermarkt)
    match_activities = SofascoreActivities(settings.database_url, sofascore_source)
    understat_activities = UnderstatActivities(
        settings.database_url, SourceInterface(understat.SOURCE, settings.understat)
    )
    identity = IdentityActivities(
        settings.database_url, settings.reep.release, covered_season=settings.covered_season
    )
    dbt = DbtActivities(settings.database_url)
    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[
            SquadIngestWorkflow,
            CompetitionSquadsWorkflow,
            MatchIngestWorkflow,
            SeasonBackfillWorkflow,
            BackfillWorkflow,
            PostMatchWorkflow,
            FixturesWorkflow,
            UnderstatIngestWorkflow,
            TransformWorkflow,
        ],
        activities=[
            activities.ingest_club_squad,
            activities.ingest_competition_clubs,
            match_activities.ingest_match,
            match_activities.tournament_seasons,
            match_activities.season_matches,
            match_activities.complete_matches,
            match_activities.upcoming_fixtures,
            match_activities.match_status,
            understat_activities.ingest_season,
            identity.resolve_identities,
            identity.create_clubs,
            dbt.build_dbt,
        ],
    )
    await worker.run()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs full request URLs; ours are logged redacted by the recording transport.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run_worker(Settings()))


if __name__ == "__main__":
    main()
