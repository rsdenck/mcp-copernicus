"""openEO API wrapper (collections, synchronous execution, batch jobs).

Endpoint: https://openeo.dataspace.copernicus.eu
Docs:     https://documentation.dataspace.copernicus.eu/APIs/openEO/openEO.html
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

from .auth import AuthError
from .config import settings

_connection: Any = None
_lock = threading.Lock()


class OpenEOError(RuntimeError):
    """Raised when the openEO backend rejects a request."""


def _credentials_ready() -> bool:
    return not settings.missing_credentials()


def connect(force: bool = False):
    """Return an authenticated, cached openEO connection."""
    global _connection
    if not _credentials_ready():
        missing = ", ".join(settings.missing_credentials())
        raise OpenEOError(f"missing environment variables: {missing}")

    with _lock:
        if _connection is not None and not force:
            return _connection

        import openeo

        try:
            connection = openeo.connect(settings.openeo_url)
            if settings.grant_type == "password":
                connection.authenticate_oidc_resource_owner_password_credentials(
                    username=settings.username,
                    password=settings.password,
                    client_id=settings.public_client_id,
                )
            else:
                connection.authenticate_oidc_client_credentials(
                    client_id=settings.client_id,
                    client_secret=settings.client_secret,
                )
        except AuthError:
            raise
        except Exception as exc:
            raise OpenEOError(f"openEO authentication failed: {exc}") from exc

        _connection = connection
        return _connection


def reset() -> None:
    global _connection
    with _lock:
        _connection = None


# ----------------------------------------------------------------------
def list_collections() -> list[dict[str, Any]]:
    connection = connect()
    try:
        collections = connection.list_collections()
    except Exception as exc:
        raise OpenEOError(f"list_collections failed: {exc}") from exc
    out = []
    for item in collections:
        out.append({
            "id": item.get("id"),
            "title": item.get("title"),
            "extent": item.get("extent"),
        })
    return out


def describe_collection(collection_id: str) -> dict[str, Any]:
    connection = connect()
    try:
        return connection.describe_collection(collection_id)
    except Exception as exc:
        raise OpenEOError(f"describe_collection({collection_id}) failed: {exc}") from exc


def list_processes(limit: int | None = None) -> list[dict[str, Any]]:
    connection = connect()
    try:
        processes = connection.list_processes()
    except Exception as exc:
        raise OpenEOError(f"list_processes failed: {exc}") from exc
    if limit:
        processes = processes[:limit]
    return [
        {
            "id": p.get("id"),
            "summary": p.get("summary"),
            "description": (p.get("description") or "")[:200],
            "categories": p.get("categories"),
            "returns": p.get("returns", {}).get("description")
            if isinstance(p.get("returns"), dict) else None,
        }
        for p in processes
    ]


# ----------------------------------------------------------------------
def _normalise_graph(graph: Any) -> dict[str, Any]:
    if isinstance(graph, str):
        if os.path.exists(graph):
            with open(graph, "r", encoding="utf-8") as handle:
                graph = json.load(handle)
        else:
            graph = json.loads(graph)
    if not isinstance(graph, dict):
        raise OpenEOError("process graph must be a dict, JSON string or file path")
    # Wrap a bare graph in "process_graph" if the caller omitted the envelope.
    if "process_graph" not in graph and any(
        isinstance(v, dict) and "process_id" in v for v in graph.values()
    ):
        graph = {"process_graph": graph}
    return graph


def execute_graph(
    graph: Any,
    timeout: int | None = None,
    dest_dir: str | None = None,
) -> dict[str, Any]:
    """Run a process graph synchronously on the backend.

    JSON answers are returned inline; binary answers (GeoTIFF, NetCDF, PNG …)
    are written to disk.
    """
    connection = connect()
    payload = _normalise_graph(graph)
    effective_timeout = timeout or settings.timeout

    try:
        response = connection.execute(
            payload, timeout=effective_timeout, auto_decode=False
        )
    except Exception as exc:
        raise OpenEOError(f"synchronous execution failed: {exc}") from exc

    if not hasattr(response, "content"):
        # Already decoded JSON.
        return {"status": "completed", "kind": "json", "result": response}

    content_type = (response.headers.get("content-type") or "").lower()
    raw = response.content
    if "json" in content_type:
        try:
            return {
                "status": "completed",
                "kind": "json",
                "result": response.json(),
            }
        except ValueError:
            pass  # fall through and store it as a file

    if getattr(response, "status_code", 200) >= 400:
        raise OpenEOError(
            f"HTTP {response.status_code}: {raw[:400].decode('utf-8', 'replace')}"
        )

    content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
    disposition = response.headers.get("content-disposition", "")
    name = _filename_from(disposition) or _extension_for(content_type)
    target_dir = dest_dir or settings.download_dir
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, name)

    with open(target, "wb") as handle:
        handle.write(raw)
    return {
        "status": "completed",
        "kind": "file",
        "path": target,
        "bytes": len(raw),
        "content_type": content_type,
    }


def _extension_for(content_type: str) -> str:
    return {
        "image/tiff": "openeo-result.tiff",
        "image/png": "openeo-result.png",
        "image/jpeg": "openeo-result.jpg",
        "application/netcdf": "openeo-result.nc",
        "application/x-netcdf": "openeo-result.nc",
    }.get(content_type, "openeo-result.bin")


def _filename_from(disposition: str) -> str:
    for part in disposition.split(";"):
        part = part.strip()
        if part.lower().startswith("filename="):
            return part.split("=", 1)[1].strip().strip('"')
    return ""


# ----------------------------------------------------------------------
def start_job(graph: Any, title: str | None = None,
              description: str | None = None) -> dict[str, Any]:
    connection = connect()
    payload = _normalise_graph(graph)
    try:
        job = connection.create_job(payload, title=title, description=description)
        job.start()
    except Exception as exc:
        raise OpenEOError(f"could not start batch job: {exc}") from exc
    return {"job_id": job.job_id, "status": job.status(), "title": title}


def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    connection = connect()
    try:
        jobs = connection.list_jobs(limit=limit)
    except Exception as exc:
        raise OpenEOError(f"list_jobs failed: {exc}") from exc
    out = []
    for job in jobs:
        out.append({
            "job_id": job.get("id"),
            "title": job.get("title"),
            "status": job.get("status"),
            "created": job.get("created"),
            "updated": job.get("updated"),
        })
    return out


def job_info(job_id: str) -> dict[str, Any]:
    connection = connect()
    try:
        job = connection.job(job_id)
        info = job.describe()
        info["status"] = job.status()
    except Exception as exc:
        raise OpenEOError(f"job {job_id}: {exc}") from exc
    return info


def job_logs(job_id: str, level: str | None = None) -> Any:
    connection = connect()
    try:
        return list(connection.job(job_id).logs(level=level))
    except Exception as exc:
        raise OpenEOError(f"logs for {job_id}: {exc}") from exc


def wait_job(
    job_id: str,
    poll_interval: float = 10.0,
    timeout: float = 3600.0,
) -> dict[str, Any]:
    """Poll a batch job until it finishes, fails or times out.

    Returns the final status together with the job description, so the caller
    can decide whether to fetch the results.
    """
    import time

    connection = connect()
    deadline = time.monotonic() + timeout
    last_status = ""
    info: dict[str, Any] = {}

    while True:
        try:
            job = connection.job(job_id)
            last_status = job.status()
            info = job.describe()
        except Exception as exc:
            raise OpenEOError(f"polling job {job_id} failed: {exc}") from exc

        if last_status in ("finished", "error", "canceled"):
            return {
                "job_id": job_id,
                "status": last_status,
                "finished": last_status == "finished",
                "elapsed_seconds": round(time.monotonic() - (deadline - timeout), 1),
                "info": info,
            }

        if time.monotonic() >= deadline:
            return {
                "job_id": job_id,
                "status": last_status,
                "finished": False,
                "timed_out": True,
                "info": info,
            }

        time.sleep(poll_interval)


def download_job_result(job_id: str, dest_dir: str | None = None) -> dict[str, Any]:
    connection = connect()
    target = dest_dir or os.path.join(settings.download_dir, job_id)
    os.makedirs(target, exist_ok=True)
    try:
        files = connection.job(job_id).download_results(target)
    except Exception as exc:
        raise OpenEOError(f"could not download results of {job_id}: {exc}") from exc
    return {
        "job_id": job_id,
        "status": connection.job(job_id).status(),
        "target": target,
        "files": [
            {"path": str(path), "bytes": meta.get("size") or meta.get("content-length")}
            for path, meta in files.items()
        ],
    }


# ----------------------------------------------------------------------
def ndvi_graph(
    collection: str,
    bbox: str,
    date_from: str,
    date_to: str,
    bands: tuple[str, str] = ("B08", "B04"),
    resolution: float | None = None,
) -> dict[str, Any]:
    """Build an NDVI process graph for a Sentinel-style collection."""
    if "," not in bbox:
        raise OpenEOError("bbox must be 'minLon,minLat,maxLon,maxLat'")
    min_lon, min_lat, max_lon, max_lat = (
        float(v) for v in bbox.replace(";", ",").split(",")
    )
    spatial_extent: dict[str, Any] = {
        "west": min_lon, "south": min_lat, "east": max_lon, "north": max_lat,
    }
    if resolution:
        spatial_extent["resolution"] = resolution

    red, nir = bands[1], bands[0]
    graph: dict[str, Any] = {
        "load": {
            "process_id": "load_collection",
            "arguments": {
                "id": collection,
                "spatial_extent": spatial_extent,
                "temporal_extent": [date_from, date_to],
                "bands": [red, nir],
            },
        },
        "ndvi": {
            "process_id": "ndvi",
            "arguments": {"data": {"from_node": "load"}, "red": red, "nir": nir},
            "result": True,
        },
    }
    return {"process_graph": graph}


def ndvi_job(
    collection: str,
    bbox: str,
    date_from: str,
    date_to: str,
    bands: tuple[str, str] = ("B08", "B04"),
    resolution: float | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    graph = ndvi_graph(collection, bbox, date_from, date_to, bands, resolution)
    return start_job(graph, title=title or f"NDVI {collection} {date_from}..{date_to}")


def capabilities() -> dict[str, Any]:
    connection = connect()
    try:
        caps = connection.capabilities()
        return {
            "url": caps.url,
            "api_version": caps.api_version(),
            "currency": caps.currency(),
            "plans": caps.list_plans(),
            "capabilities": caps.capabilities,
        }
    except Exception as exc:
        raise OpenEOError(f"capabilities failed: {exc}") from exc
