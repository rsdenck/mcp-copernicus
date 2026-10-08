"""STAC API access for Copernicus Contributing Missions (CCM) and CLMS.

The STAC catalogue covers the Copernicus Contributing Missions and the
Copernicus Land Monitoring Service — the main Sentinel collections live in
the OData catalogue instead.

Endpoint: https://catalogue.dataspace.copernicus.eu/stac
Docs:     https://documentation.dataspace.copernicus.eu/APIs/STAC.html
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import requests

from .auth import token_manager
from .config import settings

STAC_URL = "https://catalogue.dataspace.copernicus.eu/stac"


class StacError(RuntimeError):
    """Raised when the STAC API answers with an error."""


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {token_manager.get()}"}


def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{STAC_URL}/{path}"
    try:
        response = requests.get(
            url, headers=_headers(), params=params, timeout=settings.timeout
        )
    except requests.RequestException as exc:
        raise StacError(f"request to {path} failed: {exc}") from exc

    if response.status_code == 401:
        token_manager.invalidate()
        response = requests.get(
            url, headers=_headers(), params=params, timeout=settings.timeout
        )
    if response.status_code != 200:
        raise StacError(f"HTTP {response.status_code}: {response.text[:400]}")
    return response.json()


def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    url = f"{STAC_URL}/{path}"
    try:
        response = requests.post(
            url, headers={**_headers(), "Content-Type": "application/json"},
            json=body, timeout=settings.timeout,
        )
    except requests.RequestException as exc:
        raise StacError(f"request to {path} failed: {exc}") from exc

    if response.status_code == 401:
        token_manager.invalidate()
        response = requests.post(
            url, headers={**_headers(), "Content-Type": "application/json"},
            json=body, timeout=settings.timeout,
        )
    if response.status_code != 200:
        raise StacError(f"HTTP {response.status_code}: {response.text[:400]}")
    return response.json()


# ----------------------------------------------------------------------
def list_collections() -> list[dict[str, Any]]:
    body = _get("collections")
    return [
        {
            "id": c.get("id"),
            "title": c.get("title"),
            "description": (c.get("description") or "")[:200],
            "license": c.get("license"),
            "extent": c.get("extent"),
        }
        for c in body.get("collections", [])
    ]


def describe_collection(collection_id: str) -> dict[str, Any]:
    return _get(f"collections/{quote(collection_id, safe='')}")


def search(
    collections: list[str],
    datetime_from: str | None = None,
    datetime_to: str | None = None,
    bbox: str | None = None,
    limit: int = 10,
    page: int = 1,
) -> dict[str, Any]:
    """Search STAC items.

    ``datetime_from`` / ``datetime_to`` are ISO-8601; a range is written as
    ``"2026-10-01T00:00:00Z/2026-10-07T23:59:59Z"``.
    """
    if not collections:
        raise StacError("collections must not be empty")
    if limit < 1 or limit > 1000:
        raise StacError("limit must be between 1 and 1000")

    datetime_value = None
    if datetime_from and datetime_to:
        datetime_value = f"{datetime_from}/{datetime_to}"
    elif datetime_from or datetime_to:
        datetime_value = datetime_from or datetime_to

    body: dict[str, Any] = {
        "collections": collections,
        "limit": limit,
        "page": page,
    }
    if datetime_value:
        body["datetime"] = datetime_value
    if bbox:
        body["bbox"] = [float(v) for v in _parse_bbox(bbox)]

    result = _post("search", body)
    features = result.get("features", [])
    return {
        "returned": len(features),
        "matched": result.get("context", {}).get("matched"),
        "page": page,
        "limit": limit,
        "items": [_summarise_item(f) for f in features],
    }


def _parse_bbox(bbox: str) -> list[float]:
    parts = [p.strip() for p in bbox.replace(";", ",").split(",") if p.strip()]
    if len(parts) != 4:
        raise StacError(f"bbox must be 'minLon,minLat,maxLon,maxLat', got {bbox!r}")
    try:
        return [float(p) for p in parts]
    except ValueError as exc:
        raise StacError(f"bbox has non-numeric values: {bbox!r}") from exc


def _summarise_item(feature: dict[str, Any]) -> dict[str, Any]:
    assets = {
        name: {
            "href": asset.get("href"),
            "type": asset.get("type"),
            "title": asset.get("title"),
        }
        for name, asset in (feature.get("assets") or {}).items()
    }
    return {
        "id": feature.get("id"),
        "collection": feature.get("collection"),
        "datetime": feature.get("properties", {}).get("datetime"),
        "geometry": feature.get("geometry"),
        "bbox": feature.get("bbox"),
        "assets": assets,
    }


def item(collection_id: str, item_id: str) -> dict[str, Any]:
    return _get(f"collections/{quote(collection_id, safe='')}/items/{quote(item_id, safe='')}")
