"""OData catalogue access (product search, metadata, download).

API reference: https://documentation.dataspace.copernicus.eu/APIs/OData.html
"""

from __future__ import annotations

import os
from typing import Any, Iterable
from urllib.parse import quote

import requests

from .auth import AuthError, token_manager
from .config import settings

# Characters that OData expects to stay readable inside a query string.
_ODATA_SAFE = "$(),:=/'"

PRODUCT_FIELDS = (
    "Id",
    "Name",
    "S3Path",
    "ContentLength",
    "Online",
    "ContentDate",
    "ModificationDate",
    "Footprint",
    "GeoJson",
    "Checksum",
)


class CatalogueError(RuntimeError):
    """Raised when the catalogue answers with an error."""


# ----------------------------------------------------------------------
def _urlencode(pairs: Iterable[tuple[str, str]]) -> str:
    """Percent-encode a query string without touching OData syntax."""
    return "&".join(
        f"{quote(str(k), safe=_ODATA_SAFE)}={quote(str(v), safe=_ODATA_SAFE)}"
        for k, v in pairs
    )


def _odata_escape(value: str) -> str:
    """OData string literals escape a single quote by doubling it."""
    return value.replace("'", "''")


def _collection_name(collection: dict[str, Any] | None) -> str:
    if isinstance(collection, dict):
        return str(collection.get("Name") or collection.get("name") or "")
    return str(collection or "")


# ----------------------------------------------------------------------
def _filters(
    collection: str | None,
    date_from: str | None,
    date_to: str | None,
    bbox: str | None,
    wkt: str | None,
    cloud_cover_max: float | None,
    name_contains: str | None,
    extra_filters: list[str],
) -> list[str]:
    clauses: list[str] = []

    if collection:
        clauses.append(f"Collection/Name eq '{_odata_escape(collection)}'")

    if date_from:
        clauses.append(f"ContentDate/Start ge {date_from}")
    if date_to:
        clauses.append(f"ContentDate/Start le {date_to}")

    if bbox:
        points = _bbox_to_polygon(_parse_bbox(bbox))
        clauses.append(
            "OData.CSC.Intersects(area=geography'SRID=4326;"
            f"POLYGON({points})')"
        )
    elif wkt:
        geometry = wkt.replace("'", "''")
        clauses.append(
            f"OData.CSC.Intersects(area=geography'SRID=4326;{geometry}')"
        )

    if cloud_cover_max is not None:
        clauses.append(
            "Attributes/OData.CSC.DoubleAttribute/any(att:att/Name eq "
            f"'cloudCover' and att/OData.CSC.DoubleAttribute/Value le "
            f"{float(cloud_cover_max)})"
        )

    if name_contains:
        clauses.append(f"contains(Name,'{_odata_escape(name_contains)}')")

    clauses.extend(extra_filters)
    return clauses


def _parse_bbox(bbox: str) -> tuple[float, float, float, float]:
    parts = [p.strip() for p in bbox.replace(";", ",").split(",") if p.strip()]
    if len(parts) != 4:
        raise CatalogueError(
            f"bbox must be 'minLon,minLat,maxLon,maxLat', got {bbox!r}"
        )
    try:
        min_lon, min_lat, max_lon, max_lat = (float(p) for p in parts)
    except ValueError as exc:
        raise CatalogueError(f"bbox has non-numeric values: {bbox!r}") from exc
    if min_lon > max_lon or min_lat > max_lat:
        raise CatalogueError(
            f"bbox min must be lower than max: {bbox!r}"
        )
    return min_lon, min_lat, max_lon, max_lat


def _bbox_to_polygon(box: tuple[float, float, float, float]) -> str:
    min_lon, min_lat, max_lon, max_lat = box
    ring = (
        f"({min_lon} {min_lat},{max_lon} {min_lat},"
        f"{max_lon} {max_lat},{min_lon} {max_lat},{min_lon} {min_lat})"
    )
    return ring


# ----------------------------------------------------------------------
def _request(
    method: str,
    url: str,
    params: list[tuple[str, str]] | None = None,
    *,
    stream: bool = False,
    retry: bool = True,
) -> requests.Response:
    """Perform an authenticated request, forwarding Authorization by hand.

    The catalogue answers many requests with a 301 to
    ``download.dataspace.copernicus.eu``. HTTP clients strip the Authorization
    header on cross-host redirects, so the redirect is followed manually.
    """
    if params:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{_urlencode(params)}"

    token = token_manager.get()
    headers = {"Authorization": f"Bearer {token}"}
    kwargs: dict[str, Any] = {
        "headers": headers,
        "timeout": settings.timeout,
        "stream": stream,
        "allow_redirects": False,
    }
    try:
        response = requests.request(method, url, **kwargs)

        hops = 0
        while response.is_redirect or response.status_code in (301, 302, 307, 308):
            location = response.headers.get("Location")
            if not location or hops >= 3:
                break
            response.close()
            response = requests.request(method, location, **kwargs)
            hops += 1

        if response.status_code == 401 and retry:
            token_manager.invalidate()
            kwargs["headers"] = {"Authorization": f"Bearer {token_manager.get()}"}
            response = requests.request(method, url, **kwargs)
        return response
    except requests.RequestException as exc:
        raise CatalogueError(f"request to {url} failed: {exc}") from exc


def _get(path: str, params: list[tuple[str, str]] | None = None,
         stream: bool = False) -> requests.Response:
    return _request("GET", f"{settings.odata_url}/{path}", params, stream=stream)


def _raise_for_error(response: requests.Response) -> None:
    if response.status_code in (200, 206):
        return
    raise CatalogueError(
        f"HTTP {response.status_code}: {response.text[:400]}"
    )


# ----------------------------------------------------------------------
def search_products(
    collection: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    bbox: str | None = None,
    wkt: str | None = None,
    cloud_cover_max: float | None = None,
    name_contains: str | None = None,
    extra_filters: list[str] | None = None,
    limit: int = 10,
    offset: int = 0,
    order_by: str = "ContentDate/Start desc",
    with_count: bool = True,
    expand_attributes: bool = False,
) -> dict[str, Any]:
    """Run an OData product search and return a compact result set."""
    if limit < 1 or limit > 500:
        raise CatalogueError("limit must be between 1 and 500")

    clauses = _filters(
        collection, date_from, date_to, bbox, wkt,
        cloud_cover_max, name_contains, extra_filters or [],
    )
    params: list[tuple[str, str]] = []
    if clauses:
        params.append(("$filter", " and ".join(clauses)))
    if with_count:
        params.append(("$count", "true"))
    if offset:
        params.append(("$skip", str(offset)))
    params.append(("$top", str(limit)))
    if order_by:
        params.append(("$orderby", order_by))
    if expand_attributes:
        params.append(("$expand", "Attributes"))

    response = _get("Products", params)
    _raise_for_error(response)
    body = response.json()

    value = body.get("value", [])
    return {
        "count": body.get("@odata.count"),
        "returned": len(value),
        "offset": offset,
        "order_by": order_by,
        "products": [_summarise(p) for p in value],
    }


def _summarise(product: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in PRODUCT_FIELDS:
        if key in product:
            out[key] = product[key]
    for key, value in product.items():
        if key not in out and key not in ("@odata.context",):
            out[key] = value
    out.pop("@odata.context", None)
    return out


def get_product(product_id: str, expand_attributes: bool = True) -> dict[str, Any]:
    """Fetch full metadata for a single product."""
    params: list[tuple[str, str]] = []
    if expand_attributes:
        params.append(("$expand", "Attributes"))
    response = _get(f"Products({product_id})", params)
    _raise_for_error(response)
    return _summarise(response.json())


def resolve_name(name: str) -> str | None:
    """Turn a product name into its Id (exact match)."""
    params = [
        ("$filter", f"Name eq '{_odata_escape(name)}'"),
        ("$top", "1"),
    ]
    response = _get("Products", params)
    _raise_for_error(response)
    values = response.json().get("value", [])
    return str(values[0]["Id"]) if values else None


# ----------------------------------------------------------------------
def download_url(product_id: str) -> str:
    """The OData ``$value`` URL of a product (redirects to the download host)."""
    return f"{settings.odata_url}/Products({product_id})/$value"


def download_product(
    product_id: str,
    dest_dir: str | None = None,
    filename: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Download a whole product archive to disk.

    The catalogue answers with a 301 to ``download.dataspace.copernicus.eu``;
    the Authorization header is forwarded manually because HTTP clients drop it
    on cross-host redirects.
    """
    product = get_product(product_id, expand_attributes=False)
    name = filename or str(product.get("Name") or product_id)
    target_dir = dest_dir or settings.download_dir
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, name)

    if os.path.exists(target) and not overwrite:
        return {
            "status": "skipped",
            "path": target,
            "reason": "file already exists (set overwrite=true to replace)",
            "product": name,
        }

    url = download_url(product_id)
    response = _request("GET", url, stream=True)
    _raise_for_error(response)

    total = int(response.headers.get("content-length") or 0)
    written = 0
    tmp = target + ".part"
    try:
        with open(tmp, "wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 20):
                if not chunk:
                    continue
                handle.write(chunk)
                written += len(chunk)
        os.replace(tmp, target)
    finally:
        response.close()
        if os.path.exists(tmp):
            os.unlink(tmp)

    return {
        "status": "downloaded",
        "path": target,
        "bytes": written,
        "expected_bytes": total or None,
        "product": name,
        "source": url,
    }


def product_tree(product_id: str, path: str | None = None,
                 max_depth: int = 1) -> list[dict[str, Any]]:
    """List the files inside a product (``Nodes``).

    Without ``path`` this returns the package root; pass the name of a
    sub-folder (for example ``S2B_...SAFE/GRANULE``) to list its children.
    """
    endpoint = f"Products({product_id})/Nodes"
    if path:
        endpoint = f"Products({product_id})/Nodes('{_odata_escape(path)}')/Nodes"

    response = _get(endpoint)
    _raise_for_error(response)
    body = response.json()
    nodes = body.get("result") or body.get("value") or []
    if isinstance(nodes, dict):
        nodes = [nodes]

    out: list[dict[str, Any]] = []
    for node in nodes:
        entry = {
            "id": node.get("Id"),
            "name": node.get("Name"),
            "length": node.get("ContentLength"),
            "children": node.get("ChildrenNumber"),
            "uri": (node.get("Nodes") or {}).get("uri"),
        }
        out.append(entry)
        children = node.get("ChildrenNumber") or 0
        child_path = node.get("Id") or node.get("Name")
        if (
            max_depth > 1
            and children
            and child_path
        ):
            nested = f"{path}/{child_path}" if path else str(child_path)
            try:
                for item in product_tree(product_id, nested, max_depth - 1):
                    out.append(item)
            except CatalogueError:
                pass
    return out


def s3_path_of(product_id: str) -> str | None:
    """Return the object-store key of a product, if it has one."""
    product = get_product(product_id, expand_attributes=False)
    path = product.get("S3Path")
    if not path:
        return None
    path = str(path)
    prefix = f"/{settings.s3_bucket}/"
    if path.startswith(prefix):
        return path[len(prefix):]
    return path.lstrip("/")


def token_status() -> dict[str, Any]:
    try:
        return token_manager.describe()
    except AuthError as exc:  # pragma: no cover - defensive
        return {"configured": False, "error": str(exc)}
