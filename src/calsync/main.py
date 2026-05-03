from fastapi import FastAPI

from calsync import __version__

app = FastAPI(title='calsync', version=__version__)


@app.get('/healthz')
async def healthz() -> dict[str, str]:
    return {'status': 'ok', 'version': __version__}
