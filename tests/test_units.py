"""Unit tests with mocked HTTP — no credentials or network required.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

# Make the package importable when running from a fresh checkout.
sys.path.insert(0, "src")

from mcp_copernicus import catalogue, config, stac  # noqa: E402


# ----------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text
        self.headers = headers or {}
        self.is_redirect = False

    def json(self):
        return self._json

    def iter_content(self, chunk_size=1):
        yield self.text.encode()

    def close(self):
        pass


def _patch_token(monkeypatch, token="tok123"):
    monkeypatch.setattr(
        catalogue.token_manager, "get", lambda force=False: token
    )


# ======================================================================
# catalogue: filter building
# ======================================================================
class TestFilters:
    def test_collection_only(self):
        clauses = catalogue._filters("SENTINEL-2", None, None, None, None,
                                     None, None, [])
        assert clauses == ["Collection/Name eq 'SENTINEL-2'"]

    def test_date_range(self):
        clauses = catalogue._filters(
            None, "2026-01-01T00:00:00.000Z", "2026-02-01T00:00:00.000Z",
            None, None, None, None, [])
        assert "ContentDate/Start ge 2026-01-01T00:00:00.000Z" in clauses
        assert "ContentDate/Start le 2026-02-01T00:00:00.000Z" in clauses

    def test_bbox_becomes_intersects(self):
        clauses = catalogue._filters(
            None, None, None, "-48.6,-27.6,-48.5,-27.5", None, None, None, [])
        assert len(clauses) == 1
        assert clauses[0].startswith("OData.CSC.Intersects(area=geography")
        assert "POLYGON((-48.6 -27.6,-48.5 -27.6,-48.5 -27.5,-48.6 -27.5,-48.6 -27.6))" in clauses[0]

    def test_cloud_cover(self):
        clauses = catalogue._filters(
            None, None, None, None, None, 20.0, None, [])
        assert any("cloudCover" in c and "le 20.0" in c for c in clauses)

    def test_name_contains(self):
        clauses = catalogue._filters(
            None, None, None, None, None, None, "S2A_OPER", [])
        assert clauses == ["contains(Name,'S2A_OPER')"]

    def test_quotes_are_doubled(self):
        clauses = catalogue._filters("O'Brien", None, None, None, None, None, None, [])
        assert clauses == ["Collection/Name eq 'O''Brien'"]

    def test_extra_filters_appended(self):
        clauses = catalogue._filters(
            None, None, None, None, None, None, None, ["Online eq true"])
        assert clauses == ["Online eq true"]


class TestBbox:
    def test_valid(self):
        assert catalogue._parse_bbox("-48.6,-27.6,-48.5,-27.5") == (
            -48.6, -27.6, -48.5, -27.5)

    def test_semicolons(self):
        assert catalogue._parse_bbox("-48.6;-27.6;-48.5;-27.5") == (
            -48.6, -27.6, -48.5, -27.5)

    def test_wrong_arity(self):
        with pytest.raises(catalogue.CatalogueError):
            catalogue._parse_bbox("1,2,3")

    def test_non_numeric(self):
        with pytest.raises(catalogue.CatalogueError):
            catalogue._parse_bbox("a,b,c,d")

    def test_inverted(self):
        with pytest.raises(catalogue.CatalogueError):
            catalogue._parse_bbox("-48.5,-27.6,-48.6,-27.5")


# ======================================================================
# catalogue: search
# ======================================================================
class TestSearch:
    def test_builds_expected_query(self, monkeypatch):
        _patch_token(monkeypatch)
        captured = {}

        def fake_request(method, url, **kwargs):
            captured["url"] = url
            return FakeResponse(200, {"@odata.count": 1, "value": [
                {"Id": "abc", "Name": "x.SAFE", "S3Path": "/eodata/x"}
            ]})

        monkeypatch.setattr(catalogue.requests, "request", fake_request)
        result = catalogue.search_products(
            collection="SENTINEL-2",
            date_from="2026-10-01T00:00:00.000Z",
            bbox="-48.6,-27.6,-48.5,-27.5",
            cloud_cover_max=20,
            limit=5,
        )
        assert result["count"] == 1
        assert result["returned"] == 1
        url = captured["url"]
        assert "Collection/Name%20eq%20'SENTINEL-2'" in url
        assert "ContentDate/Start%20ge%202026-10-01" in url
        assert "cloudCover" in url
        assert "$top=5" in url
        assert "$count=true" in url

    def test_limit_bounds(self, monkeypatch):
        _patch_token(monkeypatch)
        with pytest.raises(catalogue.CatalogueError):
            catalogue.search_products(limit=0)
        with pytest.raises(catalogue.CatalogueError):
            catalogue.search_products(limit=501)

    def test_http_error_raises(self, monkeypatch):
        _patch_token(monkeypatch)
        monkeypatch.setattr(
            catalogue.requests, "request",
            lambda *a, **k: FakeResponse(400, text='{"detail":"bad"}'))
        with pytest.raises(catalogue.CatalogueError):
            catalogue.search_products(collection="SENTINEL-2")

    def test_s3_path_strips_bucket(self, monkeypatch):
        _patch_token(monkeypatch)
        monkeypatch.setattr(
            catalogue.requests, "request",
            lambda *a, **k: FakeResponse(200, {
                "Id": "abc", "Name": "x",
                "S3Path": "/eodata/Sentinel-2/MSI/x.SAFE"}))
        assert catalogue.s3_path_of("abc") == "Sentinel-2/MSI/x.SAFE"


# ======================================================================
# catalogue: download
# ======================================================================
class TestDownload:
    def test_follows_redirect_with_auth(self, monkeypatch):
        _patch_token(monkeypatch)
        calls = []

        def fake_request(method, url, **kwargs):
            calls.append((method, url, kwargs.get("headers")))
            if "catalogue" in url:
                resp = FakeResponse(301)
                resp.is_redirect = True
                resp.headers = {"Location": "https://download.example/Products(1)/$value"}
                return resp
            return FakeResponse(200, headers={"content-length": "10"},
                                text="0123456789")

        monkeypatch.setattr(catalogue.requests, "request", fake_request)
        monkeypatch.setattr(catalogue.os.path, "exists", lambda p: False)
        monkeypatch.setattr(catalogue.os, "makedirs", lambda p, exist_ok=True: None)

        class FakeFile:
            def __init__(self):
                self.data = b""
            def write(self, chunk):
                self.data += chunk
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        monkeypatch.setattr("builtins.open", lambda *a, **k: FakeFile())
        monkeypatch.setattr(catalogue.os, "replace", lambda a, b: None)

        result = catalogue.download_product("1", dest_dir="/tmp/x")
        assert result["status"] == "downloaded"
        assert result["bytes"] == 10
        # Authorization forwarded to the download host
        assert calls[1][2]["Authorization"] == "Bearer tok123"

    def test_skips_existing(self, monkeypatch):
        _patch_token(monkeypatch)
        monkeypatch.setattr(
            catalogue.requests, "request",
            lambda *a, **k: FakeResponse(200, {"Id": "1", "Name": "x.SAFE"}))
        monkeypatch.setattr(catalogue.os.path, "exists", lambda p: True)
        result = catalogue.download_product("1", dest_dir="/tmp/x")
        assert result["status"] == "skipped"


# ======================================================================
# STAC
# ======================================================================
class TestStac:
    def test_search_builds_body(self, monkeypatch):
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["body"] = kwargs["json"]
            return FakeResponse(200, {"features": [], "context": {"matched": 0}})

        monkeypatch.setattr(stac.requests, "post", fake_post)
        monkeypatch.setattr(stac.token_manager, "get", lambda force=False: "t")

        stac.search(["ccm-optical"], datetime_from="2026-10-01T00:00:00Z",
                    datetime_to="2026-10-07T00:00:00Z",
                    bbox="-48.6,-27.6,-48.5,-27.5", limit=3)
        body = captured["body"]
        assert body["collections"] == ["ccm-optical"]
        assert body["datetime"] == "2026-10-01T00:00:00Z/2026-10-07T00:00:00Z"
        assert body["bbox"] == [-48.6, -27.6, -48.5, -27.5]
        assert body["limit"] == 3

    def test_requires_collections(self, monkeypatch):
        with pytest.raises(stac.StacError):
            stac.search([])

    def test_limit_bounds(self, monkeypatch):
        with pytest.raises(stac.StacError):
            stac.search(["ccm-optical"], limit=0)

    def test_summarises_assets(self, monkeypatch):
        def fake_post(url, **kwargs):
            return FakeResponse(200, {"features": [{
                "id": "i1", "collection": "ccm-optical",
                "properties": {"datetime": "2026-10-01T00:00:00Z"},
                "assets": {"data": {"href": "https://x/y.tif",
                                    "type": "image/tiff"}},
            }]})
        monkeypatch.setattr(stac.requests, "post", fake_post)
        monkeypatch.setattr(stac.token_manager, "get", lambda force=False: "t")
        out = stac.search(["ccm-optical"])
        assert out["items"][0]["assets"]["data"]["href"] == "https://x/y.tif"


# ======================================================================
# config
# ======================================================================
class TestConfig:
    def test_mask_hides_secret(self, monkeypatch):
        monkeypatch.setenv("CDSE_CLIENT_ID", "sh-5be9fd0b-3b31-4d11-a751")
        monkeypatch.setenv("CDSE_CLIENT_SECRET", "supersecretvalue")
        s = config.Settings()
        masked = s.mask()
        assert "supersecretvalue" not in json.dumps(masked)
        assert masked["client_id"] == "sh-5be…"
        assert "client_secret" not in masked

    def test_missing_credentials(self, monkeypatch):
        for var in ("CDSE_CLIENT_ID", "CDSE_CLIENT_SECRET",
                    "CDSE_USERNAME", "CDSE_PASSWORD"):
            monkeypatch.delenv(var, raising=False)
        s = config.Settings()
        assert s.grant_type == "client_credentials"
        assert set(s.missing_credentials()) == {
            "CDSE_CLIENT_ID", "CDSE_CLIENT_SECRET"}

    def test_password_grant_detected(self, monkeypatch):
        monkeypatch.setenv("CDSE_USERNAME", "u")
        monkeypatch.setenv("CDSE_PASSWORD", "p")
        s = config.Settings()
        assert s.grant_type == "password"
        assert s.missing_credentials() == []
