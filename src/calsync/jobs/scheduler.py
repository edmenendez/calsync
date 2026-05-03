"""APScheduler wiring.

Three jobs per the design:

- renew_expiring_channels   every 6 hours
- reconcile_all             every 1 hour (backstop for missed webhooks)
- (TODO window-rollover)    daily at 00:05 local; meaningful only once
                            date_window is bounded in production config
"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from calsync.config import Settings
from calsync.jobs.reconcile import reconcile_all
from calsync.jobs.renew_channels import renew_expiring_channels

log = logging.getLogger(__name__)


def create_scheduler(settings: Settings) -> AsyncIOScheduler:
    """Create (but do not start) the production scheduler.

    Caller (FastAPI lifespan) is responsible for calling .start() and
    .shutdown(wait=False) at appropriate points.
    """
    sched = AsyncIOScheduler(timezone='UTC')

    sched.add_job(
        renew_expiring_channels,
        'interval',
        hours=6,
        id='renew_channels',
        replace_existing=True,
        kwargs={'settings': settings},
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        reconcile_all,
        'interval',
        hours=1,
        id='reconcile',
        replace_existing=True,
        kwargs={'settings': settings},
        max_instances=1,
        coalesce=True,
    )

    return sched
