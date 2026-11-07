"""FastAPI application: control UI, JSON API and radio stream proxy."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from .audio import SILENCE_SECONDS, SilenceCache
from .config import Settings
from .db import Database
from .icecast import IcecastServer
from .manager import StationManager
from .models import StationCreate, StationImport, StationUpdate, station_slug

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "info").strip().upper() or "INFO",
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("ytradio")

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# Headers worth preserving from Icecast when proxying a stream.
UPSTREAM_HEADERS = (
    "icy-name",
    "icy-genre",
    "icy-description",
    "icy-url",
    "icy-br",
    "icy-sr",
    "icy-pub",
    "icy-metaint",
)


def _header_safe(value: str) -> str:
    return value.encode("latin-1", "replace").decode("latin-1")


def _apply_security_headers(response: Response) -> Response:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; media-src 'self'; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    )
    return response


def _check_basic_auth(header: str, username: str, password: str) -> bool:
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    supplied_user, separator, supplied_password = decoded.partition(":")
    if not separator:
        return False
    user_ok = secrets.compare_digest(supplied_user, username)
    password_ok = secrets.compare_digest(supplied_password, password)
    return user_ok & password_ok


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.load()
    db = Database(settings.db_path)
    db.init()
    icecast = IcecastServer(settings)
    await icecast.start()
    manager = StationManager(settings, db, icecast)
    await manager.startup()
    silence_cache = SilenceCache(settings)
    stream_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=20),
    )

    app.state.settings = settings
    app.state.db = db
    app.state.icecast = icecast
    app.state.manager = manager
    app.state.silence_cache = silence_cache
    app.state.stream_client = stream_client

    logger.info(
        "YT Radio is ready on port %s (data: %s)", settings.port, settings.data_dir
    )
    try:
        yield
    finally:
        await manager.shutdown()
        await stream_client.aclose()
        await icecast.stop()


app = FastAPI(
    title="YT Radio",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.middleware("http")
async def gate_requests(request: Request, call_next):
    settings: Settings = request.app.state.settings
    path = request.url.path
    is_stream = path.startswith("/stream/")
    is_health = path == "/healthz"

    if settings.app_password and not (is_stream or is_health):
        authorization = request.headers.get("authorization", "")
        if not _check_basic_auth(
            authorization, settings.app_username, settings.app_password
        ):
            return _apply_security_headers(
                Response(
                    content="Authentication required.\n",
                    status_code=401,
                    media_type="text/plain",
                    headers={
                        "WWW-Authenticate": 'Basic realm="YT Radio", charset="UTF-8"'
                    },
                )
            )

    if (
        settings.app_password
        and request.method in MUTATING_METHODS
        and path.startswith("/api/")
        and request.headers.get("x-requested-with") != "yt-radio"
    ):
        return _apply_security_headers(
            JSONResponse(
                {"detail": "Missing X-Requested-With header."}, status_code=403
            )
        )

    return _apply_security_headers(await call_next(request))


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_manager(request: Request) -> StationManager:
    return request.app.state.manager


def _station_or_404(db: Database, station_id: int):
    station = db.get_station(station_id)
    if station is None:
        raise HTTPException(status_code=404, detail="Station not found.")
    return station


@app.get("/", include_in_schema=False)
async def index(request: Request) -> FileResponse:
    settings: Settings = request.app.state.settings
    return FileResponse(
        settings.static_dir / "index.html",
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> RedirectResponse:
    return RedirectResponse("/static/favicon.svg", status_code=307)


@app.get("/healthz")
async def healthz(request: Request) -> dict[str, object]:
    manager: StationManager = request.app.state.manager
    return {
        "status": "ok" if manager.icecast_ok else "degraded",
        "icecast": manager.icecast_ok,
        "error": manager.icecast_error,
    }


@app.get("/api/stations")
async def list_stations(request: Request) -> list[dict[str, object]]:
    db = get_db(request)
    manager = get_manager(request)
    return [manager.view(station) for station in db.list_stations()]


@app.post("/api/stations", status_code=201)
async def create_station(
    payload: StationCreate, request: Request
) -> JSONResponse:
    db = get_db(request)
    manager = get_manager(request)

    slug = db.unique_slug(station_slug(payload.name))
    station = db.create_station(
        name=payload.name,
        url=payload.url,
        slug=slug,
        codec=payload.codec,
        on_demand=payload.on_demand,
        desired_state=payload.autostart,
    )
    if payload.autostart and not payload.on_demand:
        await manager.start(station)
    logger.info("Created station %s (%s)", station.name, station.slug)
    return JSONResponse(manager.view(station), status_code=201)


@app.get("/api/export")
async def export_stations(request: Request) -> JSONResponse:
    db = get_db(request)
    stations = db.list_stations()
    exported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = {
        "version": 1,
        "exported_at": exported_at,
        "stations": [
            {
                "name": station.name,
                "url": station.url,
                "slug": station.slug,
                "codec": station.codec,
                "on_demand": station.on_demand,
                "enabled": station.desired_state,
            }
            for station in stations
        ],
    }
    filename = (
        "yt-radio-stations-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    )
    return JSONResponse(
        payload,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/import")
async def import_stations(
    payload: StationImport, request: Request
) -> dict[str, object]:
    db = get_db(request)
    manager = get_manager(request)

    if payload.replace:
        for station in db.list_stations():
            await manager.stop(station.id)
            manager.clear_logs(station.id)
        db.delete_all_stations()

    created = []
    try:
        for item in payload.stations:
            base_slug = item.slug or station_slug(item.name)
            station = db.create_station(
                name=item.name,
                url=item.url,
                slug=db.unique_slug(base_slug),
                codec=item.codec,
                on_demand=item.on_demand,
                desired_state=item.enabled,
            )
            created.append(station)
    except Exception:
        for station in created:
            db.delete_station(station.id)
            manager.clear_logs(station.id)
        raise

    for station in created:
        if station.desired_state and not station.on_demand:
            await manager.start(station)

    logger.info(
        "Imported %s stations (replace=%s)", len(created), payload.replace
    )
    return {
        "imported": len(created),
        "replaced": payload.replace,
        "stations": [manager.view(station) for station in created],
    }


@app.get("/api/stations/{station_id}")
async def get_station(station_id: int, request: Request) -> dict[str, object]:
    db = get_db(request)
    manager = get_manager(request)
    return manager.view(_station_or_404(db, station_id))


@app.patch("/api/stations/{station_id}")
async def update_station(
    station_id: int, payload: StationUpdate, request: Request
) -> dict[str, object]:
    db = get_db(request)
    manager = get_manager(request)
    existing = _station_or_404(db, station_id)
    was_active = manager.is_active(station_id)
    changes = payload.model_dump(exclude_unset=True)
    new_codec = changes.get("codec", existing.codec)
    new_on_demand = changes.get("on_demand", existing.on_demand)
    if new_codec == "aac_he" and new_on_demand:
        raise HTTPException(
            status_code=422,
            detail="HE-AAC copy is always-on only. Choose AAC copy for on-demand.",
        )

    updated = db.update_station(station_id, **changes)
    if updated is None:
        raise HTTPException(status_code=404, detail="Station not found.")

    if was_active:
        await manager.stop(station_id)
    if updated.desired_state and not updated.on_demand:
        await manager.start(updated)

    logger.info("Updated station %s", existing.slug)
    return manager.view(updated)


@app.post("/api/stations/{station_id}/start")
async def start_station(station_id: int, request: Request) -> dict[str, object]:
    db = get_db(request)
    manager = get_manager(request)
    _station_or_404(db, station_id)
    db.set_desired_state(station_id, True)
    station = _station_or_404(db, station_id)
    if not station.on_demand:
        await manager.start(station)
    return manager.view(station)


@app.post("/api/stations/{station_id}/stop")
async def stop_station(station_id: int, request: Request) -> dict[str, object]:
    db = get_db(request)
    manager = get_manager(request)
    _station_or_404(db, station_id)
    db.set_desired_state(station_id, False)
    await manager.stop(station_id)
    station = _station_or_404(db, station_id)
    return manager.view(station)


@app.post("/api/stations/{station_id}/restart")
async def restart_station(station_id: int, request: Request) -> dict[str, object]:
    db = get_db(request)
    manager = get_manager(request)
    _station_or_404(db, station_id)
    db.set_desired_state(station_id, True)
    station = _station_or_404(db, station_id)
    await manager.restart(station)
    return manager.view(station)


@app.delete("/api/stations/{station_id}", status_code=204)
async def delete_station(station_id: int, request: Request) -> Response:
    db = get_db(request)
    manager = get_manager(request)
    _station_or_404(db, station_id)
    await manager.stop(station_id)
    manager.clear_logs(station_id)
    db.delete_station(station_id)
    logger.info("Deleted station id=%s", station_id)
    return Response(status_code=204)


@app.get("/api/stations/{station_id}/logs")
async def station_logs(station_id: int, request: Request) -> dict[str, object]:
    db = get_db(request)
    manager = get_manager(request)
    station = _station_or_404(db, station_id)
    view = manager.view(station)
    return {
        "lines": manager.get_logs(station_id),
        "status": view["status"],
        "last_error": view["last_error"],
    }


def _station_content_type(station) -> str:
    return "audio/aacp" if station.codec == "aac_he" else "audio/aac"


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        client = forwarded.split(",", 1)[0].strip()
        if client:
            return client[:128]
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _stream_station(db: Database, filename: str):
    if not filename.endswith(".aac"):
        raise HTTPException(status_code=404, detail="Not found.")
    slug = filename[:-4]
    if not SLUG_PATTERN.fullmatch(slug):
        raise HTTPException(status_code=404, detail="Not found.")
    station = db.get_station_by_slug(slug)
    if station is None:
        raise HTTPException(status_code=404, detail="Not found.")
    return station


def _stream_response_headers(
    station, upstream: httpx.Response | None = None
) -> dict[str, str]:
    headers: dict[str, str] = {
        "Content-Type": _station_content_type(station),
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Access-Control-Allow-Origin": "*",
        "X-Accel-Buffering": "no",
    }
    if upstream is not None:
        for name in UPSTREAM_HEADERS:
            value = upstream.headers.get(name)
            if value is not None:
                headers[name] = value
    headers.setdefault("icy-name", _header_safe(station.name))
    headers.setdefault("icy-description", "YouTube Live radio")
    headers.setdefault("icy-genre", "Live")
    return headers


def _upstream_request_headers(
    request: Request, allow_metadata: bool
) -> dict[str, str]:
    headers: dict[str, str] = {}
    if allow_metadata and request.headers.get("icy-metadata"):
        headers["Icy-MetaData"] = "1"
    user_agent = request.headers.get("user-agent")
    if user_agent:
        headers["User-Agent"] = user_agent
    return headers


async def _open_icecast_stream(
    client: httpx.AsyncClient,
    settings: Settings,
    station,
    headers: dict[str, str],
) -> httpx.Response | None:
    upstream_url = f"{settings.icecast_url}{station.stream_path}"
    try:
        upstream_request = client.build_request(
            "GET", upstream_url, headers=headers
        )
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        logger.debug("Could not reach Icecast for %s: %s", station.slug, exc)
        return None

    if upstream.status_code != 200:
        await upstream.aclose()
        return None
    return upstream


async def _relay_body(upstream: httpx.Response, slug: str, lease=None):
    try:
        async for chunk in upstream.aiter_raw():
            if chunk:
                yield chunk
    except asyncio.CancelledError:
        raise
    except httpx.HTTPError as exc:
        logger.info("Stream proxy for %s ended: %s", slug, exc)
    finally:
        with contextlib.suppress(Exception):
            await upstream.aclose()
        if lease is not None:
            await lease.release()


async def _tuning_body(
    manager: StationManager,
    client: httpx.AsyncClient,
    settings: Settings,
    station,
    request_headers: dict[str, str],
    silence: bytes,
    lease,
):
    """Stream silent tuning audio until the on-demand source is up."""

    upstream: httpx.Response | None = None
    interval = SILENCE_SECONDS if silence else 0.5
    deadline = time.monotonic() + settings.on_demand_start_timeout

    try:
        while upstream is None:
            if not manager.listener_allowed(lease.runtime):
                return

            upstream = await _open_icecast_stream(
                client, settings, station, request_headers
            )
            if upstream is not None:
                break

            if time.monotonic() >= deadline:
                logger.warning(
                    "On-demand station %s did not start within %ss",
                    station.slug,
                    settings.on_demand_start_timeout,
                )
                return

            if silence:
                yield silence
            await asyncio.sleep(interval)

        async for chunk in upstream.aiter_raw():
            if chunk:
                yield chunk
    except asyncio.CancelledError:
        raise
    except httpx.HTTPError as exc:
        logger.info("On-demand stream %s ended: %s", station.slug, exc)
    finally:
        if upstream is not None:
            with contextlib.suppress(Exception):
                await upstream.aclose()
        await lease.release()


@app.get("/stream/{filename}", include_in_schema=False)
async def proxy_stream(filename: str, request: Request) -> Response:
    settings: Settings = request.app.state.settings
    db = get_db(request)
    manager = get_manager(request)
    station = _stream_station(db, filename)

    client: httpx.AsyncClient = request.app.state.stream_client
    lease = await manager.acquire_listener(
        station.id, _client_key(request)
    )
    if lease is None:
        raise HTTPException(status_code=503, detail="This station is turned off.")

    if station.on_demand:
        try:
            upstream = await _open_icecast_stream(
                client,
                settings,
                station,
                _upstream_request_headers(request, allow_metadata=True),
            )
            if upstream is not None:
                return StreamingResponse(
                    _relay_body(upstream, station.slug, lease),
                    status_code=200,
                    headers=_stream_response_headers(station, upstream),
                    background=BackgroundTask(lease.release),
                )

            silence = await request.app.state.silence_cache.get()
            return StreamingResponse(
                _tuning_body(
                    manager,
                    client,
                    settings,
                    station,
                    _upstream_request_headers(request, allow_metadata=False),
                    silence,
                    lease,
                ),
                status_code=200,
                headers=_stream_response_headers(station),
                background=BackgroundTask(lease.release),
            )
        except Exception:
            await lease.release()
            raise

    upstream = await _open_icecast_stream(
        client,
        settings,
        station,
        _upstream_request_headers(request, allow_metadata=True),
    )
    if upstream is None:
        await lease.release()
        raise HTTPException(
            status_code=503, detail="The stream is not available yet."
        )

    return StreamingResponse(
        _relay_body(upstream, station.slug, lease),
        status_code=200,
        headers=_stream_response_headers(station, upstream),
        background=BackgroundTask(lease.release),
    )


@app.head("/stream/{filename}", include_in_schema=False)
async def stream_head(filename: str, request: Request) -> Response:
    db = get_db(request)
    station = _stream_station(db, filename)
    if not station.desired_state:
        raise HTTPException(status_code=503, detail="This station is turned off.")
    return Response(status_code=200, headers=_stream_response_headers(station))


app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).resolve().parent / "static"),
    name="static",
)
