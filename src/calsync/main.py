from contextlib import asynccontextmanager

from fastapi import FastAPI

from calsync import __version__
from calsync.api.admin import router as admin_router
from calsync.api.oauth import router as oauth_router
from calsync.api.webhook import router as webhook_router
from calsync.db import init_db
from calsync.deps import get_settings
from calsync.jobs.scheduler import create_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_db(settings.db_path)
    scheduler = create_scheduler(settings)
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title='calsync', version=__version__, lifespan=lifespan)
app.include_router(oauth_router)
app.include_router(webhook_router)
app.include_router(admin_router)


@app.get('/healthz')
async def healthz() -> dict[str, str]:
    return {'status': 'ok', 'version': __version__}
