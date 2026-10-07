"""Smoke test: exercise every mcp-copernicus tool against the live APIs."""
from __future__ import annotations

import json
import os
import sys
import time

from mcp_copernicus import server

BBOX = "-48.6,-27.6,-48.5,-27.5"   # litoral de Santa Catarina
RESULTS: list[tuple[str, dict]] = []


def run(label: str, fn, *args, **kwargs) -> dict:
    start = time.time()
    try:
        out = fn(*args, **kwargs)
    except Exception as exc:  # pragma: no cover
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    elapsed = time.time() - start
    RESULTS.append((label, out if isinstance(out, dict) else {"value": out}))
    summary = json.dumps(out, default=str)
    flag = "OK  " if (isinstance(out, dict) and out.get("ok", True)) else "FAIL"
    print(f"[{flag}] {label:34s} {elapsed:6.1f}s  {summary[:220]}", flush=True)
    return out if isinstance(out, dict) else {}


def main() -> int:
    print("=" * 100)

    status = run("copernicus_status", server.copernicus_status)

    search = run(
        "search S2 + bbox + cloud<=20",
        server.copernicus_search_products,
        collection="SENTINEL-2",
        date_from="2026-10-01T00:00:00.000Z",
        date_to="2026-10-07T23:59:59.000Z",
        bbox=BBOX,
        cloud_cover_max=20,
        limit=3,
        expand_attributes=True,
    )
    products = (search.get("products") or [])
    if not products:
        print("!! nenhuma busca retornou produto; abortando testes dependentes")
        first_id = None
    else:
        first_id = products[0].get("Id")
        print(f"    -> {len(products)} produtos, count={search.get('count')}, "
              f"primeiro={products[0].get('Name')}")

    if first_id:
        run("get_product", server.copernicus_get_product, first_id)
        run("product_s3_path", server.copernicus_product_s3_path, first_id)
        run("list_product_files", server.copernicus_list_product_files, first_id)

    # um produto pequeno (AUX/GIP, ~450 kB) p/ testar download sem baixar 300 MB
    small = run(
        "search produto pequeno (GIP)",
        server.copernicus_search_products,
        collection="SENTINEL-2",
        name_contains="GIP",
        date_from="2026-10-01T00:00:00.000Z",
        limit=1,
        with_count=False,
    )
    small_id = (small.get("products") or [{}])[0].get("Id")
    if small_id:
        run("download_product",
            server.copernicus_download_product,
            product_id=small_id,
            dest_dir="/tmp/opencode/copernicus-test")
        run("product_tree depth=2",
            server.copernicus_list_product_files,
            small_id, None, 2)

    run("s3_list", server.copernicus_s3_list,
        prefix="Sentinel-2/AUX/GIP_R2EQOG_B06/2026/10/08/", max_keys=5)
    if small_id:
        run("s3_head", server.copernicus_s3_head,
            key="Sentinel-2/AUX/GIP_R2EQOG_B06/2026/10/08/")

    run("openeo_collections", server.openeo_collections)
    run("openeo_describe_collection", server.openeo_describe_collection, "SENTINEL2_L2A")

    graph = run(
        "openeo_ndvi_graph (build)",
        server.openeo_ndvi_graph,
        collection="SENTINEL2_L2A",
        bbox=BBOX,
        date_from="2026-10-01",
        date_to="2026-10-03",
        resolution=100,
    )
    if graph.get("process_graph"):
        run("openeo_execute_graph (sync NDVI)",
            server.openeo_execute_graph,
            graph=graph,
            dest_dir="/tmp/opencode/copernicus-test",
            timeout=180)

    run("openeo_jobs", server.openeo_jobs, limit=5)

    print("=" * 100)
    ok = sum(1 for _, r in RESULTS if r.get("ok", False))
    fail = len(RESULTS) - ok
    print(f"RESULTADO: {ok} ok / {fail} falha de {len(RESULTS)} chamadas")

    with open("/tmp/opencode/mcp-copernicus-smoke.json", "w") as fh:
        json.dump({label: res for label, res in RESULTS}, fh, indent=2, default=str)
    print("detalhes -> /tmp/opencode/mcp-copernicus-smoke.json")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
