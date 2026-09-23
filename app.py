"""Widget server: every README image is a URL rendered on request."""

import os

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route


async def health(request):
    return JSONResponse({
        "ok": True,
        "commit": os.environ.get("VERCEL_GIT_COMMIT_SHA", "local")[:7],
    }, headers={"Cache-Control": "no-store"})


app = Starlette(routes=[Route("/api/health", health)])
