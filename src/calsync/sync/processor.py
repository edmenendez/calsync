"""Per-event sync orchestrator.

Given one source event from a webhook delta, drive the fan-out matrix:
for each configured (target, mode) pair, run skip filters, then either
ensure the mirror exists with current content (creates/adopts/repairs
+ patches) or delete existing mirrors (cancelled / now-skip events).

Returns a structured summary suitable for logging and for the
/admin/dryrun-week response.
"""

import contextlib
import datetime as dt
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Literal

from calsync.config import DateWindow, NosyncConfig
from calsync.gapi.client import GoogleCalendarClient
from calsync.gapi.errors import GoneError, NotFoundError
from calsync.repositories import event_links
from calsync.sync.filters import is_cancelled, is_mirror, should_skip
from calsync.sync.matrix import PERSONAL_ACCOUNT_LABEL, Mode, targets_for_source
from calsync.sync.mirror import CalendarRef, ensure_mirror
from calsync.sync.payload import build_mirror_payload

log = logging.getLogger(__name__)


@dataclass
class TargetOutcome:
    target_label: str
    mode: Mode
    action: Literal['skipped', 'created', 'updated', 'deleted', 'noop', 'failed']
    reason: str = ''
    link_id: str | None = None


@dataclass
class ProcessOutcome:
    source_account: str
    source_event_id: str
    targets: list[TargetOutcome] = field(default_factory=list)


async def process_event(
    *,
    conn: sqlite3.Connection,
    source: CalendarRef,
    source_event: dict,
    targets_by_label: dict[str, CalendarRef],
    clients_by_label: dict[str, GoogleCalendarClient],
    hmac_key: str,
    nosync_config: NosyncConfig,
    date_window: DateWindow,
    now: dt.datetime,
    matrix: list[tuple[str, str, Mode]] | None = None,
    color_map: dict[str, str] | None = None,
    dry_run: bool = False,
) -> ProcessOutcome:
    """Fan out one source event to every configured target.

    `targets_by_label` and `clients_by_label` are constructed by the
    webhook handler, which has already refreshed access tokens for each
    account. The orchestrator never touches the token-refresh layer.
    """
    outcome = ProcessOutcome(
        source_account=source.account_label,
        source_event_id=source_event.get('id', '<missing>'),
    )

    # Top-level echo guard: never process a calsync mirror as a source.
    if is_mirror(source_event):
        outcome.targets.append(TargetOutcome('*', 'busy', 'skipped', reason='echo_loop_calsync_origin'))
        return outcome

    cancelled = is_cancelled(source_event)

    for target_label, mode in targets_for_source(source.account_label, matrix):
        target = targets_by_label.get(target_label)
        client = clients_by_label.get(target_label)
        if target is None or client is None:
            outcome.targets.append(TargetOutcome(target_label, mode, 'skipped', 'no_target_account_configured'))
            continue

        if cancelled:
            await _delete_mirrors_for_source(
                conn=conn,
                client=client,
                source=source,
                target=target,
                source_event_id=source_event['id'],
                outcome=outcome,
                mode=mode,
                dry_run=dry_run,
            )
            continue

        skip = should_skip(
            source_event,
            source_account_label=source.account_label,
            target_account_label=target_label,
            personal_account_label=PERSONAL_ACCOUNT_LABEL,
            mode=mode,
            nosync_config=nosync_config,
            date_window=date_window,
            now=now,
        )
        if skip.skip:
            # If we already have a mirror for this source on this target but the source
            # has now become ineligible (e.g., moved out of the window, declined, tagged
            # [nosync]), we need to delete the stale mirror.
            existing_link = event_links.find_by_source_target(
                conn,
                source_calendar_id=source.db_id,
                source_event_id=source_event['id'],
                mirror_calendar_id=target.db_id,
            )
            if existing_link:
                await _delete_mirror_pair(
                    conn=conn,
                    client=client,
                    target=target,
                    link=existing_link,
                    outcome=outcome,
                    mode=mode,
                    target_label=target_label,
                    delete_reason=f'now_skipped:{skip.reason}',
                    dry_run=dry_run,
                )
            else:
                outcome.targets.append(TargetOutcome(target_label, mode, 'skipped', skip.reason))
            continue

        # Eligible: ensure mirror exists, then patch with current source state.
        if dry_run:
            outcome.targets.append(TargetOutcome(target_label, mode, 'noop', 'dry_run'))
            continue

        try:
            link_id = await ensure_mirror(
                conn=conn,
                client=client,
                hmac_key=hmac_key,
                source=source,
                target=target,
                source_event=source_event,
                mode=mode,
                color_map=color_map,
            )
            link = event_links.find_by_link_id(conn, link_id)
            assert link is not None
            mirror_key = link['mirror_key']

            payload = build_mirror_payload(
                source_event=source_event,
                mode=mode,
                mirror_key=mirror_key,
                source_account_label=source.account_label,
                color_map=color_map,
            )
            try:
                await client.patch_event(target.google_calendar_id, link['mirror_event_id'], payload)
                outcome.targets.append(TargetOutcome(target_label, mode, 'updated', link_id=link_id))
            except (NotFoundError, GoneError):
                # Mirror disappeared between ensure and patch (rare race or manual delete).
                # Fall back to a fresh ensure_mirror, which will hit Path 1b stale-DB recreate.
                link_id = await ensure_mirror(
                    conn=conn,
                    client=client,
                    hmac_key=hmac_key,
                    source=source,
                    target=target,
                    source_event=source_event,
                    mode=mode,
                    color_map=color_map,
                )
                outcome.targets.append(TargetOutcome(target_label, mode, 'created', link_id=link_id))
        except Exception as e:  # noqa: BLE001  - we want to log and continue per target
            log.exception('sync failed for source=%s target=%s: %s', source.account_label, target_label, e)
            outcome.targets.append(TargetOutcome(target_label, mode, 'failed', reason=type(e).__name__))

    return outcome


async def _delete_mirrors_for_source(
    *,
    conn: sqlite3.Connection,
    client: GoogleCalendarClient,
    source: CalendarRef,
    target: CalendarRef,
    source_event_id: str,
    outcome: ProcessOutcome,
    mode: Mode,
    dry_run: bool,
) -> None:
    """Delete the mirror for one (source, target) when the source was deleted."""
    link = event_links.find_by_source_target(
        conn,
        source_calendar_id=source.db_id,
        source_event_id=source_event_id,
        mirror_calendar_id=target.db_id,
    )
    if link is None:
        outcome.targets.append(TargetOutcome(target.account_label, mode, 'noop', 'no_mirror_to_delete'))
        return
    await _delete_mirror_pair(
        conn=conn,
        client=client,
        target=target,
        link=link,
        outcome=outcome,
        mode=mode,
        target_label=target.account_label,
        delete_reason='source_cancelled',
        dry_run=dry_run,
    )


async def _delete_mirror_pair(
    *,
    conn: sqlite3.Connection,
    client: GoogleCalendarClient,
    target: CalendarRef,
    link: sqlite3.Row,
    outcome: ProcessOutcome,
    mode: Mode,
    target_label: str,
    delete_reason: str,
    dry_run: bool,
) -> None:
    if dry_run:
        outcome.targets.append(TargetOutcome(target_label, mode, 'noop', f'dry_run:{delete_reason}'))
        return
    try:
        # Already-gone mirrors are fine; cleaning up the DB row is what matters.
        with contextlib.suppress(NotFoundError, GoneError):
            await client.delete_event(target.google_calendar_id, link['mirror_event_id'])
        event_links.delete_by_id(conn, link['link_id'])
        outcome.targets.append(
            TargetOutcome(target_label, mode, 'deleted', reason=delete_reason, link_id=link['link_id'])
        )
    except Exception as e:  # noqa: BLE001
        log.exception('delete failed for target=%s link=%s: %s', target_label, link['link_id'], e)
        outcome.targets.append(TargetOutcome(target_label, mode, 'failed', reason=type(e).__name__))
