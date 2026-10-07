"""Configuration for mcp-copernicus, read from environment variables.

Nothing is hardcoded here: every endpoint can be overridden, and the defaults
are the public Copernicus Data Space Ecosystem endpoints documented at
https://documentation.dataspace.copernicus.eu/
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int_env(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """All knobs of the server, resolved once at import time."""

    # --- credentials -------------------------------------------------
    client_id: str = field(default_factory=lambda: _env("CDSE_CLIENT_ID"))
    client_secret: str = field(default_factory=lambda: _env("CDSE_CLIENT_SECRET"))
    username: str = field(default_factory=lambda: _env("CDSE_USERNAME"))
    password: str = field(default_factory=lambda: _env("CDSE_PASSWORD"))

    # --- endpoints ---------------------------------------------------
    token_url: str = field(default_factory=lambda: _env(
        "CDSE_TOKEN_URL",
        "https://identity.dataspace.copernicus.eu/auth/realms/CDSE"
        "/protocol/openid-connect/token",
    ))
    # Public client used by the "password" grant, as documented by CDSE.
    public_client_id: str = field(default_factory=lambda: _env(
        "CDSE_PUBLIC_CLIENT_ID", "cdse-public"))

    odata_url: str = field(default_factory=lambda: _env(
        "CDSE_ODATA_URL", "https://catalogue.dataspace.copernicus.eu/odata/v1"))
    download_url: str = field(default_factory=lambda: _env(
        "CDSE_DOWNLOAD_URL", "https://download.dataspace.copernicus.eu/odata/v1"))
    openeo_url: str = field(default_factory=lambda: _env(
        "CDSE_OPENEO_URL", "https://openeo.dataspace.copernicus.eu"))

    s3_endpoint: str = field(default_factory=lambda: _env(
        "CDSE_S3_ENDPOINT", "https://eodata.dataspace.copernicus.eu"))
    s3_bucket: str = field(default_factory=lambda: _env("CDSE_S3_BUCKET", "eodata"))
    # CDSE documents region_name="default" for this object store.
    s3_region: str = field(default_factory=lambda: _env("CDSE_S3_REGION", "default"))
    s3_keys_url: str = field(default_factory=lambda: _env(
        "CDSE_S3_KEYS_URL",
        "https://s3-keys-manager.cloudferro.com/api/user/credentials",
    ))

    # --- behaviour ---------------------------------------------------
    download_dir: str = field(default_factory=lambda: _env(
        "CDSE_DOWNLOAD_DIR", os.path.expanduser("~/copernicus-data")))
    timeout: int = field(default_factory=lambda: _int_env("CDSE_TIMEOUT", 120))
    token_refresh_skew: int = field(default_factory=lambda: _int_env(
        "CDSE_TOKEN_REFRESH_SKEW", 60))

    # ------------------------------------------------------------------
    @property
    def grant_type(self) -> str:
        """Which OAuth2 grant we will use."""
        if self.username and self.password:
            return "password"
        return "client_credentials"

    @property
    def token_client_id(self) -> str:
        return self.public_client_id if self.grant_type == "password" else self.client_id

    def missing_credentials(self) -> list[str]:
        """Names of the environment variables that still need to be set."""
        if self.grant_type == "password":
            return [k for k, v in (("CDSE_USERNAME", self.username),
                                   ("CDSE_PASSWORD", self.password)) if not v]
        return [k for k, v in (("CDSE_CLIENT_ID", self.client_id),
                               ("CDSE_CLIENT_SECRET", self.client_secret)) if not v]

    def mask(self) -> dict[str, str]:
        """Non-secret view of the configuration, for the `status` tool."""

        def hide(value: str, keep: int = 6) -> str:
            if not value:
                return ""
            return value[:keep] + "…" if len(value) > keep else "***"

        return {
            "grant_type": self.grant_type,
            "client_id": hide(self.client_id),
            "username": self.username or "",
            "token_url": self.token_url,
            "odata_url": self.odata_url,
            "download_url": self.download_url,
            "openeo_url": self.openeo_url,
            "s3_endpoint": self.s3_endpoint,
            "s3_bucket": self.s3_bucket,
            "s3_region": self.s3_region,
            "download_dir": self.download_dir,
            "timeout": str(self.timeout),
        }


settings = Settings()
