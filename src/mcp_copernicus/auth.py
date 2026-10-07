"""OAuth2 token and S3 credential management.

Tokens are short lived (30 minutes for CDSE), so both the bearer token and the
object-storage credentials are cached here and refreshed shortly before expiry.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from typing import Any

import requests

from .config import settings


class AuthError(RuntimeError):
    """Raised when CDSE refuses to hand out a token or S3 credentials."""


def _decode_claims(token: str) -> dict[str, Any]:
    """Best effort decode of the JWT payload (no signature verification)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # pragma: no cover - malformed token
        return {}


class TokenManager:
    """Fetches and caches the CDSE bearer token."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: str = ""
        self._expires_at: float = 0.0
        self._grant: str = ""

    # ------------------------------------------------------------------
    def _request(self) -> dict[str, Any]:
        data: dict[str, str] = {"grant_type": settings.grant_type}
        if settings.grant_type == "password":
            data.update({
                "client_id": settings.public_client_id,
                "username": settings.username,
                "password": settings.password,
            })
        else:
            data.update({
                "client_id": settings.client_id,
                "client_secret": settings.client_secret,
            })

        try:
            response = requests.post(
                settings.token_url, data=data, timeout=settings.timeout
            )
        except requests.RequestException as exc:
            raise AuthError(f"token endpoint unreachable: {exc}") from exc

        if response.status_code != 200:
            raise AuthError(
                f"token request failed: HTTP {response.status_code} "
                f"{response.text[:300]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise AuthError(f"token endpoint returned non-JSON: {response.text[:200]}") from exc
        token = body.get("access_token")
        if not token:
            raise AuthError(f"no access_token in response: {body}")
        return body

    # ------------------------------------------------------------------
    def get(self, force: bool = False) -> str:
        with self._lock:
            now = time.time()
            if not force and self._token and now < (
                self._expires_at - settings.token_refresh_skew
            ):
                return self._token

            body = self._request()
            self._token = body["access_token"]
            self._grant = settings.grant_type
            self._expires_at = now + float(body.get("expires_in", 1800))
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = ""
            self._expires_at = 0.0

    # ------------------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        """Token state, for the `status` tool."""
        info: dict[str, Any] = {
            "grant_type": settings.grant_type,
            "configured": not settings.missing_credentials(),
        }
        missing = settings.missing_credentials()
        if missing:
            info["missing_env"] = missing
            return info

        try:
            token = self.get()
        except AuthError as exc:
            info["error"] = str(exc)
            return info

        claims = _decode_claims(token)
        info.update({
            "token_length": len(token),
            "expires_unix": claims.get("exp"),
            "issued_unix": claims.get("iat"),
            "seconds_left": int(claims.get("exp", 0) - time.time()),
            "subject": claims.get("sub"),
            "issuer": claims.get("iss"),
            "audiences": claims.get("aud"),
            "roles": (claims.get("realm_access") or {}).get("roles", []),
        })
        return info


class S3CredentialManager:
    """Fetches and caches temporary object-storage credentials.

    CDSE issues them through the ``s3-keys-manager`` service using the same
    bearer token as every other API. The service allows at most
    ``credentialsLimit`` keys per account, so the key this server creates is
    tracked in a small state file and revoked before a new one is requested.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._creds: dict[str, str] = {}
        self._fetched_at: float = 0.0
        self._error: str = ""

    # -- persistent bookkeeping --------------------------------------
    @property
    def _state_path(self) -> str:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
        return os.path.join(base, "mcp-copernicus", "s3-credentials.json")

    def _read_state(self) -> dict[str, Any]:
        try:
            with open(self._state_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_state(self, access_id: str) -> None:
        try:
            os.makedirs(os.path.dirname(self._state_path), exist_ok=True)
            with open(self._state_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {"access_id": access_id, "created_unix": int(time.time())},
                    handle,
                )
            os.chmod(self._state_path, 0o600)
        except OSError:  # pragma: no cover - cache dir not writable
            pass

    def _clear_state(self) -> None:
        try:
            os.unlink(self._state_path)
        except OSError:
            pass

    # -- HTTP ----------------------------------------------------------
    def _headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def _create(self, token: str) -> dict[str, str]:
        try:
            response = requests.post(
                settings.s3_keys_url,
                headers=self._headers(token),
                timeout=settings.timeout,
            )
        except requests.RequestException as exc:
            raise AuthError(f"keys manager unreachable: {exc}") from exc
        if response.status_code != 200:
            raise AuthError(
                f"credential request failed: HTTP {response.status_code} "
                f"{response.text[:300]}"
            )
        body = response.json()
        if "access_id" not in body or "secret" not in body:
            raise AuthError(f"unexpected response: {json.dumps(body)[:300]}")
        return {
            "access_id": str(body["access_id"]),
            "secret": str(body["secret"]),
            "expiration_date": str(body.get("expiration_date", "")),
        }

    def _list_ids(self, token: str) -> list[dict[str, Any]]:
        try:
            response = requests.get(
                settings.s3_keys_url,
                headers=self._headers(token),
                timeout=settings.timeout,
            )
        except requests.RequestException:
            return []
        if response.status_code != 200:
            return []
        try:
            return list(response.json().get("credentials", []))
        except ValueError:
            return []

    def _revoke(self, token: str, access_id: str) -> bool:
        url = f"{settings.s3_keys_url}/access_id/{access_id}"
        try:
            response = requests.delete(
                url, headers=self._headers(token), timeout=settings.timeout
            )
        except requests.RequestException:
            return False
        return response.status_code in (200, 204)

    def _revoke_own(self, token: str) -> int:
        """Revoke keys we created before, so the account limit is respected."""
        removed = 0
        tracked = self._read_state().get("access_id")
        if tracked:
            if self._revoke(token, str(tracked)):
                removed += 1
            self._clear_state()
        return removed

    def _revoke_all(self, token: str) -> int:
        """Last resort: drop every key of this service account."""
        removed = 0
        for entry in self._list_ids(token):
            access_id = str(entry.get("access_id") or "")
            if access_id and self._revoke(token, access_id):
                removed += 1
        self._clear_state()
        return removed

    # -- public API ----------------------------------------------------
    def get(self, force: bool = False) -> dict[str, str]:
        with self._lock:
            if not force and self._creds and (time.time() - self._fetched_at) < 3600:
                return dict(self._creds)

            token = token_manager.get()

            # Release the key we issued previously (and remember the new one).
            self._revoke_own(token)

            try:
                creds = self._create(token)
            except AuthError as exc:
                if "Max number of credentials" not in str(exc):
                    self._error = str(exc)
                    raise
                # The account is full: drop everything this server may have
                # left behind and try once more.
                self._revoke_all(token)
                creds = self._create(token)

            self._write_state(creds["access_id"])
            self._creds = creds
            self._fetched_at = time.time()
            self._error = ""
            return dict(self._creds)

    def invalidate(self) -> None:
        with self._lock:
            self._creds = {}
            self._fetched_at = 0.0

    def describe(self) -> dict[str, Any]:
        try:
            creds = self.get()
        except AuthError as exc:
            return {"available": False, "error": str(exc)}
        return {
            "available": True,
            "access_id": creds.get("access_id", ""),
            "expiration_date": creds.get("expiration_date", ""),
            "fetched_unix": int(self._fetched_at),
        }


token_manager = TokenManager()
s3_credentials = S3CredentialManager()
