"""Copyright(C) 2020 PythonistaGuild

This file is part of MystBin.

MystBin is free software: you can redistribute it and / or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

MystBin is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY
without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with MystBin.  If not, see <https://www.gnu.org/licenses/>.
"""
import asyncio
import datetime
import os
import pathlib
from typing import Any, Dict

import aiohttp
import sentry_sdk
import slowapi
import ujson
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sentry_sdk.integrations.asgi import SentryAsgiMiddleware
from starlette_prometheus import metrics, PrometheusMiddleware

from routers import admin, apps, pastes, user
from utils import ratelimits
from utils.db import Database

try:
    from blackfire import (
        probe,
    )  # blackfire is used for debugging the memory heap. https://blackfire.io

    probe.initialize()
    probe.enable()
except ModuleNotFoundError:
    probe = None


class MystbinApp(FastAPI):
    """Subclassed API for Mystbin."""

    def __init__(self, *, loop: asyncio.AbstractEventLoop = None, config: pathlib.Path = None):
        loop = loop or asyncio.get_event_loop_policy().get_event_loop()
        with open(config or pathlib.Path("config.json")) as f:
            self.config: Dict[str, Dict[str, Any]] = ujson.load(f)

        super().__init__(
            title="MystBin",
            version="3.0.0",
            description="MystBin backend server",
            loop=loop,
            redoc_url="/docs",
            docs_url=None,
        )
        self.state.limiter = ratelimits.global_limiter
        self.add_exception_handler(ratelimits.RateLimitExceeded, slowapi._rate_limit_exceeded_handler)
        self.should_close = False


app = MystbinApp()


@app.middleware("http")
async def request_stats(request: Request, call_next):
    request.app.state.request_stats["total"] += 1

    if request.url.path != "/admin/stats":
        request.app.state.request_stats["latest"] = datetime.datetime.utcnow()

    response = await call_next(request)
    return response


@app.on_event("startup")
async def app_startup():
    """Async app startup."""
    app.state.db = await Database(app).__ainit__()
    app.state.client = aiohttp.ClientSession()
    app.state.request_stats = {"total": 0, "latest": datetime.datetime.utcnow()}
    app.state.webhook_url = app.config["sentry"].get("discord_webhook", None)

    if __name__ == "__main__":  # for testing
        from utils.cli import CLIHandler

        app.state.cliserver = CLIHandler(app)
        asyncio.get_event_loop().create_task(app.state.cliserver.parse_cli())


if probe is not None:

    @app.on_event("shutdown")
    async def app_shutdown():
        probe.end()


app.include_router(admin.router)
app.include_router(apps.router)
app.include_router(pastes.router)
app.include_router(user.router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        app.config["site"]["frontend_site"],
        app.config["site"]["backend_site"],
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


try:
    sentry_dsn = app.config["sentry"]["dsn"]
except KeyError:
    pass
else:
    traces_sample_rate = app.config["sentry"].get("traces_sample_rate", 0.3)
    sentry_sdk.init(dsn=sentry_dsn, traces_sample_rate=traces_sample_rate, attach_stacktrace=True)

    app.add_middleware(SentryAsgiMiddleware)

app.add_middleware(PrometheusMiddleware)
app.add_route("/metrics/", metrics)


class UvicornServer(uvicorn.Server):
    # this is already a part of uvicorn, however we need to be able to close the app ourselves via the cli
    # so this is subclassed to insert `or self.config.app.should_close`
    async def main_loop(self) -> None:
        counter = 0
        should_exit = await self.on_tick(counter)
        while not should_exit:
            counter += 1
            counter = counter % 864000
            await asyncio.sleep(0.1)
            should_exit = await self.on_tick(counter) or self.config.app.should_close  # type: ignore


if __name__ == "__main__":
    if os.environ.get("ISDOCKER") is not None:
        config = uvicorn.Config(app, port=app.config["site"]["backend_port"], host="0.0.0.0")
        # allow from all hosts when in a docker container, so that requests can be proxied in
    else:
        config = uvicorn.Config(app, port=app.config["site"]["backend_port"], host="127.0.0.1")
    
    server = UvicornServer(config)
    server.run()
