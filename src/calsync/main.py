from contextlib import asynccontextmanager

from fastapi import FastAPI

from calsync import __version__
from calsync.api.oauth import router as oauth_router
from calsync.db import init_db
from calsync.deps import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_db(settings.db_path)
    yield


app = FastAPI(title='calsync', version=__version__, lifespan=lifespan)
app.include_router(oauth_router)


@app.get('/healthz')
async def healthz() -> dict[str, str]:
    return {'status': 'ok', 'version': __version__}
