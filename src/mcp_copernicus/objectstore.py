"""Object storage access (S3-compatible) for the Copernicus EO data repository.

Endpoint: https://eodata.dataspace.copernicus.eu  (bucket ``eodata``)
Credentials: temporary keys issued by the ``s3-keys-manager`` service.

Known limitation observed with OAuth *service-account* clients: the keys
issued by the API are rejected by the object store with
``SignatureDoesNotMatch``. Tools here surface the exact server error instead
of hiding it, so the caller can act on it (for example by supplying keys
generated in the CDSE web UI through ``CDSE_S3_ACCESS_KEY_ID`` /
``CDSE_S3_SECRET_ACCESS_KEY``).
"""

from __future__ import annotations

import os
from typing import Any

from .auth import AuthError, s3_credentials
from .config import settings


class ObjectStoreError(RuntimeError):
    """Raised when the object store refuses a request."""


def _s3_client():
    import boto3
    import botocore

    access_key, secret_key = _keys()
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=settings.s3_region,
        config=botocore.config.Config(
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=30,
            read_timeout=settings.timeout,
        ),
    )


def _keys() -> tuple[str, str]:
    """Explicit keys win; otherwise ask the keys manager."""
    explicit_id = os.environ.get("CDSE_S3_ACCESS_KEY_ID", "").strip()
    explicit_secret = os.environ.get("CDSE_S3_SECRET_ACCESS_KEY", "").strip()
    if explicit_id and explicit_secret:
        return explicit_id, explicit_secret

    try:
        creds = s3_credentials.get()
    except AuthError as exc:
        raise ObjectStoreError(str(exc)) from exc
    return creds["access_id"], creds["secret"]


def _describe_error(exc: Exception) -> dict[str, Any]:
    """Turn a botocore error into something a caller can act on."""
    info: dict[str, Any] = {
        "endpoint": settings.s3_endpoint,
        "bucket": settings.s3_bucket,
        "region": settings.s3_region,
        "error_type": type(exc).__name__,
    }
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error") or {}
        meta = response.get("ResponseMetadata") or {}
        headers = meta.get("HTTPHeaders") or {}
        info.update({
            "code": error.get("Code", ""),
            "message": error.get("Message", ""),
            "http_status": meta.get("HTTPStatusCode"),
            "request_id": meta.get("RequestId", ""),
            "proxy": headers.get("server", ""),
        })
    else:
        info["message"] = str(exc)[:500]

    code = str(info.get("code", ""))
    message = str(info.get("message", ""))
    status = info.get("http_status")

    if code == "SignatureDoesNotMatch":
        info["hint"] = (
            "The object store knows this access_id but not this secret: the "
            "key pair issued to an OAuth service account is not accepted by "
            "the eodata store. Generate keys in the CDSE web UI "
            "(https://eodata-s3keysmanager.dataspace.copernicus.eu/) and set "
            "CDSE_S3_ACCESS_KEY_ID / CDSE_S3_SECRET_ACCESS_KEY."
        )
    elif code == "InvalidAccessKeyId":
        info["hint"] = "The key has not propagated yet, or it was deleted."
    elif status == 403 and (not message or message.lower() == "forbidden"):
        info["hint"] = (
            "The edge proxy rejected the request without a body. An empty "
            "403 from this endpoint has been observed together with "
            "SignatureDoesNotMatch answers from the store itself, both when "
            "using credentials issued to an OAuth service account. Use keys "
            "generated in the CDSE web UI (CDSE_S3_ACCESS_KEY_ID / "
            "CDSE_S3_SECRET_ACCESS_KEY), or download through the catalogue "
            "with copernicus_download_product."
        )
    return info


# ----------------------------------------------------------------------
def list_prefix(
    prefix: str = "",
    delimiter: str = "/",
    max_keys: int = 100,
    include_dirs: bool = True,
) -> dict[str, Any]:
    """List objects (and common prefixes) under ``prefix`` in bucket ``eodata``."""
    if max_keys < 1 or max_keys > 1000:
        raise ObjectStoreError("max_keys must be between 1 and 1000")

    prefix = prefix.lstrip("/")
    if prefix.startswith(f"{settings.s3_bucket}/"):
        prefix = prefix[len(settings.s3_bucket) + 1:]

    client = _s3_client()
    kwargs: dict[str, Any] = {
        "Bucket": settings.s3_bucket,
        "MaxKeys": max_keys,
    }
    if prefix:
        kwargs["Prefix"] = prefix
    if delimiter:
        kwargs["Delimiter"] = delimiter

    try:
        response = client.list_objects_v2(**kwargs)
    except Exception as exc:  # botocore raises ClientError and friends
        raise ObjectStoreError(
            f"listing failed: {_describe_error(exc)}"
        ) from exc

    objects = [
        {"key": item["Key"], "size": item["Size"],
         "last_modified": item["LastModified"].isoformat()}
        for item in response.get("Contents", [])
    ]
    directories = [p["Prefix"] for p in response.get("CommonPrefixes", [])] \
        if include_dirs else []

    return {
        "bucket": settings.s3_bucket,
        "prefix": prefix,
        "delimiter": delimiter,
        "is_truncated": bool(response.get("IsTruncated")),
        "next_token": response.get("NextContinuationToken"),
        "directories": directories,
        "objects": objects,
    }


def head(key: str) -> dict[str, Any]:
    """Metadata of a single object."""
    key = _normalise_key(key)
    client = _s3_client()
    try:
        response = client.head_object(Bucket=settings.s3_bucket, Key=key)
    except Exception as exc:
        raise ObjectStoreError(f"head failed: {_describe_error(exc)}") from exc
    return {
        "key": key,
        "size": response.get("ContentLength"),
        "content_type": response.get("ContentType"),
        "last_modified": response.get("LastModified").isoformat(),
        "etag": response.get("ETag"),
    }


def download(
    key: str,
    dest: str | None = None,
    overwrite: bool = False,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    """Download one object from the EO data repository."""
    key = _normalise_key(key)
    info = head(key)
    size = int(info.get("size") or 0)

    if max_bytes is not None and size > max_bytes:
        raise ObjectStoreError(
            f"object is {size} bytes, larger than max_bytes={max_bytes}"
        )

    if dest is None:
        dest = os.path.join(settings.download_dir, os.path.basename(key))
    dest = os.path.abspath(dest)
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)

    if os.path.exists(dest) and not overwrite:
        return {
            "status": "skipped",
            "key": key,
            "path": dest,
            "reason": "file already exists (set overwrite=true to replace)",
            "bytes": os.path.getsize(dest),
        }

    client = _s3_client()
    tmp = dest + ".part"
    try:
        client.download_file(settings.s3_bucket, key, tmp)
        os.replace(tmp, dest)
    except Exception as exc:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise ObjectStoreError(f"download failed: {_describe_error(exc)}") from exc

    return {
        "status": "downloaded",
        "key": key,
        "path": dest,
        "bytes": os.path.getsize(dest),
        "size": size,
    }


def _normalise_key(key: str) -> str:
    key = (key or "").strip().lstrip("/")
    bucket_prefix = f"{settings.s3_bucket}/"
    if key.startswith(bucket_prefix):
        key = key[len(bucket_prefix):]
    if not key:
        raise ObjectStoreError("key must not be empty")
    return key


def status() -> dict[str, Any]:
    """Report credentials and reachability without hiding failures."""
    info: dict[str, Any] = {
        "endpoint": settings.s3_endpoint,
        "bucket": settings.s3_bucket,
        "region": settings.s3_region,
        "explicit_keys": bool(
            os.environ.get("CDSE_S3_ACCESS_KEY_ID")
            and os.environ.get("CDSE_S3_SECRET_ACCESS_KEY")
        ),
    }
    try:
        info["credentials"] = s3_credentials.describe()
    except Exception as exc:  # pragma: no cover - defensive
        info["credentials"] = {"available": False, "error": str(exc)}

    # A cheap probe: list a tiny prefix and record exactly what happened.
    try:
        list_prefix(prefix="", delimiter="/", max_keys=1)
        info["reachable"] = True
        info["probe"] = "ok"
    except ObjectStoreError as exc:
        info["reachable"] = False
        info["probe"] = str(exc)
    return info
