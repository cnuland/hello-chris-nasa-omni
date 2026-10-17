"""Object store (RustFS / S3) behind an interface.

Stored URIs are ``s3://<bucket>/<key>``; the API exposes them as ``/media/<key>``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

MAX_KEY_LENGTH = 1024
STREAM_CHUNK_BYTES = 64 * 1024


class ObjectNotFound(KeyError):
    """Raised when a key does not exist in the object store."""


class InvalidKey(ValueError):
    """Raised for keys that are empty, absolute or contain path traversal segments."""


class ObjectStoreError(Exception):
    """The object store refused the request (e.g. AccessDenied); the API answers 502."""


class ObjectStoreUnavailable(ObjectStoreError):
    """The object store could not be reached; the API answers 503."""


@dataclass(frozen=True)
class StoredObject:
    """An object being read: ``chunks`` yields the body piece by piece and closes the source when exhausted."""

    chunks: Iterator[bytes]
    content_type: str
    content_length: int | None = None

    @property
    def data(self) -> bytes:
        """Read the whole body (convenience for small objects and tests; consumes ``chunks``)."""
        return b"".join(self.chunks)


class ObjectStore(Protocol):
    bucket: str

    def put(self, key: str, data: bytes, content_type: str) -> str:
        """Store bytes; return the ``s3://bucket/key`` URI."""

    def get(self, key: str) -> StoredObject:
        """Return the (streaming) object or raise ObjectNotFound / InvalidKey / ObjectStoreError."""

    def delete(self, key: str) -> None:
        """Best-effort delete (used to clean up after a failed transaction)."""


def validate_key(key: str) -> str:
    """Return the key if safe, raise InvalidKey otherwise.

    Rejected: empty keys, keys longer than 1024 chars, a leading '/', backslashes, control characters and any empty,
    '.' or '..' path segment (so 'a//b', 'a/./b' and 'a/../b' are all refused).
    """
    if not isinstance(key, str) or not key:
        raise InvalidKey("empty key")
    if len(key) > MAX_KEY_LENGTH:
        raise InvalidKey("key too long")
    if key.startswith("/"):
        raise InvalidKey("absolute key")
    if "\\" in key:
        raise InvalidKey("backslash in key")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in key):
        raise InvalidKey("control character in key")
    for segment in key.split("/"):
        if segment in ("", ".", ".."):
            raise InvalidKey("unsafe path segment in key")
    return key


def make_uri(bucket: str, key: str) -> str:
    return f"s3://{bucket}/{key}"


def uri_to_key(uri: str | None, bucket: str) -> str | None:
    """Return the object key for ``s3://<bucket>/<key>`` (our bucket only), else None."""
    if not uri:
        return None
    prefix = f"s3://{bucket}/"
    if not uri.startswith(prefix):
        return None
    key = uri[len(prefix) :]
    try:
        return validate_key(key)
    except InvalidKey:
        return None


def uri_to_url(uri: str | None, bucket: str) -> str | None:
    """Map ``s3://<bucket>/<key>`` to ``/media/<key>``; None stays None.

    URIs that are already API paths (``/media/...``) are passed through. URIs for other buckets or with unsafe keys
    map to None, because ``/media`` could not serve them.
    """
    if not uri:
        return None
    if uri.startswith("/media/"):
        return uri
    key = uri_to_key(uri, bucket)
    return f"/media/{key}" if key is not None else None


class S3ObjectStore:
    """boto3-backed implementation (RustFS speaks S3)."""

    def __init__(self, client: Any, bucket: str):
        self.client = client
        self.bucket = bucket

    @classmethod
    def from_settings(cls, settings: Any) -> S3ObjectStore:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3, "mode": "standard"}),
        )
        return cls(client, settings.s3_bucket)

    def put(self, key: str, data: bytes, content_type: str) -> str:
        validate_key(key)
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        return make_uri(self.bucket, key)

    def get(self, key: str) -> StoredObject:
        validate_key(key)
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in ("NoSuchKey", "404", "NotFound", "NoSuchBucket"):
                raise ObjectNotFound(key) from exc
            logger.error("object store refused get", extra={"key": key, "code": code})
            raise ObjectStoreError(code or "object store error") from exc
        except BotoCoreError as exc:
            logger.error("object store unreachable", extra={"key": key, "error": str(exc)})
            raise ObjectStoreUnavailable(str(exc)) from exc
        body = resp["Body"]
        length = resp.get("ContentLength")
        return StoredObject(
            chunks=_stream(body),
            content_type=resp.get("ContentType") or "application/octet-stream",
            content_length=int(length) if isinstance(length, int) else None,
        )

    def delete(self, key: str) -> None:
        validate_key(key)
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception:  # pragma: no cover - best effort cleanup
            logger.warning("object delete failed", extra={"key": key}, exc_info=True)


def _stream(body: Any) -> Iterator[bytes]:
    """Yield a boto StreamingBody in chunks and always close it (also when the client disconnects)."""
    try:
        yield from body.iter_chunks(chunk_size=STREAM_CHUNK_BYTES)
    finally:
        body.close()
