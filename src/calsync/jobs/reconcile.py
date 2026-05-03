"""Hourly backstop reconciliation.

Google warns that "a small percentage" of webhook notifications drop.
This job pulls a syncToken delta on every calendar regardless of
webhook activity, catching anything missed. Cheap because syncToken
returns only deltas - empty if nothing changed.

Runs under the per-calendar lock so it serializes against any in-flight
webhook handler for the same calendar.
"""

import datetime as dt
import logging

from calsync.config import DateWindow, NosyncConfig, Settings
from calsync.db import connect
from calsync.sync.context import build_world
from calsync.sync.delta import pull_and_process
from calsync.sync.locks import get_calendar_lock

log = logging.getLogger(__name__)


async def reconcile_all(settings: Settings) -> dict:
    """Pull syncToken deltas for every calendar; return a summary."""
    now = dt.datetime.now(dt.UTC)
    summary: dict[str, dict] = {}

    with connect(settings.db_path) as conn:
        targets, clients = await build_world(conn, settings)

        for label, ref in targets.items():
            async with get_calendar_lock(ref.db_id):
                try:
                    outcomes = await pull_and_process(
                        conn=conn,
                        source=ref,
                        source_client=clients[label],
                        targets_by_label=targets,
                        clients_by_label=clients,
                        hmac_key=settings.mirror_hmac_key,
                        nosync_config=NosyncConfig(),
                        date_window=DateWindow(mode='all'),
                        now=now,
                        dry_run=settings.dry_run,
                    )
                    summary[label] = {'events_seen': len(outcomes), 'error': None}
                except Exception as e:  # noqa: BLE001
                    log.exception('reconciliation failed for %s', label)
                    summary[label] = {'events_seen': 0, 'error': type(e).__name__}

    log.info('reconciliation summary: %s', summary)
    return summary
