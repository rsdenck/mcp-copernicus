"""mcp-copernicus — MCP server exposing the Copernicus Data Space Ecosystem.

Tools are grouped in three areas:

* catalogue (OData): search, inspect and download products
* object storage (S3): list and download from the ``eodata`` bucket
* openEO: collections, synchronous processing and batch jobs
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from . import __version__
from .auth import token_manager
from .catalogue import (
    CatalogueError,
    download_product,
    get_product,
    product_tree,
    search_products,
    s3_path_of,
)
from .config import settings
from .objectstore import ObjectStoreError, download as s3_download, head as s3_head, list_prefix
from . import openeo_api
from . import stac as stac_api

mcp = MCPServer(
    name="mcp-copernicus",
    instructions=(
        "Copernicus Data Space Ecosystem tools: catalogue search (OData), "
        "object storage and openEO processing. Dates are ISO-8601 "
        "(e.g. 2026-09-01T00:00:00.000Z), bounding boxes are "
        "'minLon,minLat,maxLon,maxLat' in WGS84."
    ),
)


# ----------------------------------------------------------------------
def _fail(exc: Exception, area: str) -> dict[str, Any]:
    """Every tool reports failures as data, so callers can react to them."""
    payload: dict[str, Any] = {
        "ok": False,
        "area": area,
        "error": str(exc),
        "error_type": type(exc).__name__,
    }
    return payload


def _ok(**kwargs: Any) -> dict[str, Any]:
    payload = {"ok": True}
    payload.update(kwargs)
    return payload


# ======================================================================
# 1. status
# ======================================================================
@mcp.tool(
    name="copernicus_status",
    description=(
        "Report the configured Copernicus endpoints, the bearer-token state "
        "(expiry, roles) and whether object-storage credentials work. Use this "
        "first when something fails."
    ),
)
def copernicus_status() -> dict[str, Any]:
    return _ok(
        version=__version__,
        config=settings.mask(),
        auth=token_manager.describe(),
        object_store=_objectstore_status(),
        openeo=_openeo_status(),
    )


def _objectstore_status() -> dict[str, Any]:
    from .objectstore import status as store_status
    try:
        return store_status()
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}


def _openeo_status() -> dict[str, Any]:
    info: dict[str, Any] = {"url": settings.openeo_url}
    try:
        connection = openeo_api.connect()
        info["authenticated"] = True
        info["api_version"] = getattr(connection, "api_version", None)
        info["capabilities_url"] = settings.openeo_url
    except Exception as exc:
        info["authenticated"] = False
        info["error"] = str(exc)
    return info


# ======================================================================
# 2. catalogue
# ======================================================================
@mcp.tool(
    name="copernicus_search_products",
    description=(
        "Search the Copernicus catalogue (OData). Filters: collection name "
        "(e.g. 'SENTINEL-2'), ISO date range, WGS84 bbox, maximum cloud "
        "cover (0-100), substring of the product name, plus raw OData "
        "clauses via extra_filters. Returns metadata including Id and "
        "S3Path for every hit."
    ),
)
def copernicus_search_products(
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
    try:
        result = search_products(
            collection=collection,
            date_from=date_from,
            date_to=date_to,
            bbox=bbox,
            wkt=wkt,
            cloud_cover_max=cloud_cover_max,
            name_contains=name_contains,
            extra_filters=extra_filters,
            limit=limit,
            offset=offset,
            order_by=order_by,
            with_count=with_count,
            expand_attributes=expand_attributes,
        )
        return _ok(**result)
    except Exception as exc:
        return _fail(exc, "catalogue")


@mcp.tool(
    name="copernicus_get_product",
    description=(
        "Fetch full metadata for one product by its Id (GUID). Includes "
        "S3Path, size, content date, footprint and attributes such as cloud "
        "cover."
    ),
)
def copernicus_get_product(product_id: str, expand_attributes: bool = True) -> dict[str, Any]:
    try:
        return _ok(product=get_product(product_id, expand_attributes=expand_attributes))
    except Exception as exc:
        return _fail(exc, "catalogue")


@mcp.tool(
    name="copernicus_download_product",
    description=(
        "Download a product archive to disk via the catalogue redirect. "
        "Pass either a product Id (GUID) or a exact product Name. Files "
        "already present are skipped unless overwrite is true. Returns the "
        "local path and byte count."
    ),
)
def copernicus_download_product(
    product_id: str | None = None,
    name: str | None = None,
    dest_dir: str | None = None,
    filename: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    try:
        if not product_id:
            if not name:
                return _fail(CatalogueError("provide product_id or name"), "catalogue")
            resolved = None
            from .catalogue import resolve_name
            resolved = resolve_name(name)
            if not resolved:
                return _fail(CatalogueError(f"no product named {name!r}"), "catalogue")
            product_id = resolved
        return _ok(**download_product(
            product_id, dest_dir=dest_dir, filename=filename, overwrite=overwrite
        ))
    except Exception as exc:
        return _fail(exc, "catalogue")


@mcp.tool(
    name="copernicus_list_product_files",
    description=(
        "List the contents of a product (OData Nodes): the package root by "
        "default, or a sub-folder when 'path' is given (e.g. "
        "'S2B_xxx.SAFE/GRANULE'). Increase max_depth to walk further down."
    ),
)
def copernicus_list_product_files(
    product_id: str,
    path: str | None = None,
    max_depth: int = 1,
) -> dict[str, Any]:
    try:
        files = product_tree(product_id, path=path, max_depth=max_depth)
        return _ok(product_id=product_id, path=path or "",
                   count=len(files), files=files)
    except Exception as exc:
        return _fail(exc, "catalogue")


@mcp.tool(
    name="copernicus_product_s3_path",
    description=(
        "Return the object-store key (relative to the eodata bucket) of a "
        "product, ready to pass to s3_list / s3_download."
    ),
)
def copernicus_product_s3_path(product_id: str) -> dict[str, Any]:
    try:
        key = s3_path_of(product_id)
        if not key:
            return _fail(CatalogueError("product has no S3Path"), "catalogue")
        return _ok(product_id=product_id, key=key,
                   bucket=settings.s3_bucket)
    except Exception as exc:
        return _fail(exc, "catalogue")


# ======================================================================
# 3. object storage
# ======================================================================
@mcp.tool(
    name="copernicus_s3_list",
    description=(
        "List objects and common prefixes in the Copernicus 'eodata' bucket. "
        "prefix is relative to the bucket root, e.g. "
        "'Sentinel-2/L1C/2026/10/06/'. Delimiter '/' yields folders."
    ),
)
def copernicus_s3_list(
    prefix: str = "",
    delimiter: str = "/",
    max_keys: int = 100,
) -> dict[str, Any]:
    try:
        return _ok(**list_prefix(prefix=prefix, delimiter=delimiter, max_keys=max_keys))
    except Exception as exc:
        return _fail(exc, "object_store")


@mcp.tool(
    name="copernicus_s3_head",
    description="Return size, content type and ETag of a single object in the eodata bucket.",
)
def copernicus_s3_head(key: str) -> dict[str, Any]:
    try:
        return _ok(**s3_head(key))
    except Exception as exc:
        return _fail(exc, "object_store")


@mcp.tool(
    name="copernicus_s3_download",
    description=(
        "Download one object from the eodata bucket to a local path. "
        "Optional max_bytes guards against unexpectedly large files."
    ),
)
def copernicus_s3_download(
    key: str,
    dest: str | None = None,
    overwrite: bool = False,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    try:
        return _ok(**s3_download(key, dest=dest, overwrite=overwrite, max_bytes=max_bytes))
    except Exception as exc:
        return _fail(exc, "object_store")


# ======================================================================
# 4. openEO
# ======================================================================
@mcp.tool(
    name="openeo_collections",
    description="List the data collections available on the CDSE openEO backend.",
)
def openeo_collections() -> dict[str, Any]:
    try:
        items = openeo_api.list_collections()
        return _ok(count=len(items), collections=items)
    except Exception as exc:
        return _fail(exc, "openeo")


@mcp.tool(
    name="openeo_describe_collection",
    description=(
        "Full description of one openEO collection: bands, temporal and "
        "spatial extent, license and process parameters."
    ),
)
def openeo_describe_collection(collection_id: str) -> dict[str, Any]:
    try:
        return _ok(collection=openeo_api.describe_collection(collection_id))
    except Exception as exc:
        return _fail(exc, "openeo")


@mcp.tool(
    name="openeo_execute_graph",
    description=(
        "Run a process graph synchronously. Accepts a dict, a JSON string or "
        "a path to a JSON file. JSON answers are returned inline; binary "
        "results (GeoTIFF, NetCDF, PNG) are written to dest_dir. Prefer a "
        "batch job (openeo_start_job) for anything beyond a small area."
    ),
)
def openeo_execute_graph(
    graph: Any,
    dest_dir: str | None = None,
    timeout: int | None = None,
) -> dict[str, Any]:
    try:
        return _ok(**openeo_api.execute_graph(graph, timeout=timeout, dest_dir=dest_dir))
    except Exception as exc:
        return _fail(exc, "openeo")


@mcp.tool(
    name="openeo_start_job",
    description=(
        "Start an asynchronous openEO batch job from a process graph and "
        "return its job id. Poll with openeo_job_info, then fetch the "
        "results with openeo_download_job_result."
    ),
)
def openeo_start_job(
    graph: Any,
    title: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    try:
        return _ok(**openeo_api.start_job(graph, title=title, description=description))
    except Exception as exc:
        return _fail(exc, "openeo")


@mcp.tool(
    name="openeo_jobs",
    description="List your openEO batch jobs with their current status.",
)
def openeo_jobs(limit: int = 50) -> dict[str, Any]:
    try:
        jobs = openeo_api.list_jobs(limit=limit)
        return _ok(count=len(jobs), jobs=jobs)
    except Exception as exc:
        return _fail(exc, "openeo")


@mcp.tool(
    name="openeo_job_info",
    description="Status, parameters, result assets and error messages of one batch job.",
)
def openeo_job_info(job_id: str) -> dict[str, Any]:
    try:
        return _ok(**openeo_api.job_info(job_id))
    except Exception as exc:
        return _fail(exc, "openeo")


@mcp.tool(
    name="openeo_job_logs",
    description="Fetch the log lines of a batch job (optionally filtered by level).",
)
def openeo_job_logs(job_id: str, level: str | None = None) -> dict[str, Any]:
    try:
        logs = openeo_api.job_logs(job_id, level=level)
        return _ok(job_id=job_id, count=len(logs), logs=logs)
    except Exception as exc:
        return _fail(exc, "openeo")


@mcp.tool(
    name="openeo_download_job_result",
    description="Download every result file of a finished batch job into a local directory.",
)
def openeo_download_job_result(job_id: str, dest_dir: str | None = None) -> dict[str, Any]:
    try:
        return _ok(**openeo_api.download_job_result(job_id, dest_dir=dest_dir))
    except Exception as exc:
        return _fail(exc, "openeo")


@mcp.tool(
    name="openeo_ndvi_job",
    description=(
        "Start a batch job that computes NDVI for a bbox and date range. "
        "bands defaults to Sentinel-2 (B08, B04). Returns the job id to poll."
    ),
)
def openeo_ndvi_job(
    collection: str,
    bbox: str,
    date_from: str,
    date_to: str,
    nir_band: str = "B08",
    red_band: str = "B04",
    resolution: float | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    try:
        return _ok(**openeo_api.ndvi_job(
            collection, bbox, date_from, date_to,
            bands=(nir_band, red_band), resolution=resolution, title=title,
        ))
    except Exception as exc:
        return _fail(exc, "openeo")


@mcp.tool(
    name="openeo_ndvi_graph",
    description=(
        "Build (without running) an NDVI process graph for a bbox and date "
        "range, so you can inspect or amend it before passing it to "
        "openeo_execute_graph or openeo_start_job."
    ),
)
def openeo_ndvi_graph(
    collection: str,
    bbox: str,
    date_from: str,
    date_to: str,
    nir_band: str = "B08",
    red_band: str = "B04",
    resolution: float | None = None,
) -> dict[str, Any]:
    try:
        graph = openeo_api.ndvi_graph(
            collection, bbox, date_from, date_to,
            bands=(nir_band, red_band), resolution=resolution,
        )
        return _ok(**graph)
    except Exception as exc:
        return _fail(exc, "openeo")


@mcp.tool(
    name="openeo_list_processes",
    description=(
        "List the openEO processes available on the backend (load_collection, "
        "ndvi, save_result, ...) with a short summary of each."
    ),
)
def openeo_list_processes(limit: int | None = None) -> dict[str, Any]:
    try:
        processes = openeo_api.list_processes(limit=limit)
        return _ok(count=len(processes), processes=processes)
    except Exception as exc:
        return _fail(exc, "openeo")


@mcp.tool(
    name="openeo_capabilities",
    description=(
        "Backend capabilities: openEO version, billing plans, endpoints and "
        "supported file formats."
    ),
)
def openeo_capabilities() -> dict[str, Any]:
    try:
        return _ok(**openeo_api.capabilities())
    except Exception as exc:
        return _fail(exc, "openeo")


@mcp.tool(
    name="openeo_wait_job",
    description=(
        "Block until a batch job finishes, fails or times out, polling every "
        "poll_interval seconds. Returns the final status and job info; fetch "
        "the outputs afterwards with openeo_download_job_result."
    ),
)
def openeo_wait_job(
    job_id: str,
    poll_interval: float = 10.0,
    timeout: float = 3600.0,
) -> dict[str, Any]:
    try:
        return _ok(**openeo_api.wait_job(
            job_id, poll_interval=poll_interval, timeout=timeout
        ))
    except Exception as exc:
        return _fail(exc, "openeo")


# ======================================================================
# 5. STAC (Copernicus Contributing Missions / CLMS)
# ======================================================================
@mcp.tool(
    name="copernicus_stac_collections",
    description=(
        "List the STAC collections: Copernicus Contributing Missions (CCM) "
        "and CLMS products. The main Sentinel collections are in the OData "
        "catalogue instead."
    ),
)
def copernicus_stac_collections() -> dict[str, Any]:
    try:
        collections = stac_api.list_collections()
        return _ok(count=len(collections), collections=collections)
    except Exception as exc:
        return _fail(exc, "stac")


@mcp.tool(
    name="copernicus_stac_search",
    description=(
        "Search STAC items by collection, ISO date range and WGS84 bbox. "
        "Returns item ids, datetimes and asset URLs."
    ),
)
def copernicus_stac_search(
    collections: list[str],
    datetime_from: str | None = None,
    datetime_to: str | None = None,
    bbox: str | None = None,
    limit: int = 10,
    page: int = 1,
) -> dict[str, Any]:
    try:
        result = stac_api.search(
            collections=collections,
            datetime_from=datetime_from,
            datetime_to=datetime_to,
            bbox=bbox,
            limit=limit,
            page=page,
        )
        return _ok(**result)
    except Exception as exc:
        return _fail(exc, "stac")


# ======================================================================
# 6. pagination helper
# ======================================================================
@mcp.tool(
    name="copernicus_search_all",
    description=(
        "Run the same search as copernicus_search_products but follow the "
        "OData nextLink chain and return every page. Use a small limit per "
        "page for large result sets."
    ),
)
def copernicus_search_all(
    collection: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    bbox: str | None = None,
    wkt: str | None = None,
    cloud_cover_max: float | None = None,
    name_contains: str | None = None,
    extra_filters: list[str] | None = None,
    page_size: int = 50,
    max_pages: int = 20,
    order_by: str = "ContentDate/Start desc",
    expand_attributes: bool = False,
) -> dict[str, Any]:
    from .catalogue import search_products

    all_products: list[dict[str, Any]] = []
    total: int | None = None
    offset = 0

    for _ in range(max_pages):
        page = search_products(
            collection=collection,
            date_from=date_from,
            date_to=date_to,
            bbox=bbox,
            wkt=wkt,
            cloud_cover_max=cloud_cover_max,
            name_contains=name_contains,
            extra_filters=extra_filters,
            limit=page_size,
            offset=offset,
            order_by=order_by,
            with_count=(total is None),
            expand_attributes=expand_attributes,
        )
        if total is None:
            total = page.get("count")
        products = page.get("products") or []
        all_products.extend(products)
        if len(products) < page_size:
            break
        offset += page_size

    return _ok(
        count=total,
        returned=len(all_products),
        pages=(offset // page_size) + 1,
        products=all_products,
    )


# ======================================================================
def main() -> None:
    """Console-script entry point."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
