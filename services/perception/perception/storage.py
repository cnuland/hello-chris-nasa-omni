"""AC-PER-13 object storage (RustFS / S3).

Keys: ``frames/<camera>/<YYYY-MM-DD>/<ts>.jpg``, ``crops/<camera>/<YYYY-MM-DD>/<ts>-<n>.jpg``,
``raw/<camera>/<YYYY-MM-DD>/<ts>.json``; ``<ts>`` is the UTC time ``HHMMSS.ffffff`` (no '/' or ':').
``put`` returns ``s3://<bucket>/<key>`` (the inventory-api URI form).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Protocol

_SAFE = re.compile(r"[^0-9A-Za-z._-]+")


class Storage(Protocol):
    bucket: str

    def put(self, key: str, data: bytes, content_type: str) -> str: ...


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def _segment(value: str) -> str:
    return _SAFE.sub("_", value).strip("_") or "unknown"


def _prefix(kind: str, camera_id: str, ts: datetime) -> tuple[str, str]:
    t = _utc(ts)
    return f"{kind}/{_segment(camera_id)}/{t:%Y-%m-%d}", f"{t:%H%M%S.%f}"


def frame_key(camera_id: str, ts: datetime) -> str:
    prefix, stamp = _prefix("frames", camera_id, ts)
    return f"{prefix}/{stamp}.jpg"


def crop_key(camera_id: str, ts: datetime, n: int) -> str:
    prefix, stamp = _prefix("crops", camera_id, ts)
    return f"{prefix}/{stamp}-{int(n)}.jpg"


def raw_key(camera_id: str, ts: datetime) -> str:
    prefix, stamp = _prefix("raw", camera_id, ts)
    return f"{prefix}/{stamp}.json"


class S3Storage:
    """boto3-backed object store (RustFS is S3-compatible)."""

    def __init__(self, client: Any, bucket: str):
        self.client = client
        self.bucket = bucket

    @classmethod
    def from_config(cls, config: Any) -> S3Storage:
        import boto3  # local import: boto3 is slow to import and only needed for the real store

        kwargs: dict[str, Any] = {"region_name": getattr(config, "s3_region", None) or "us-east-1"}
        if getattr(config, "s3_endpoint", None):
            kwargs["endpoint_url"] = config.s3_endpoint
        if getattr(config, "s3_access_key", None):
            kwargs["aws_access_key_id"] = config.s3_access_key
        if getattr(config, "s3_secret_key", None):
            kwargs["aws_secret_access_key"] = config.s3_secret_key
        return cls(boto3.client("s3", **kwargs), config.s3_bucket)

    def put(self, key: str, data: bytes, content_type: str) -> str:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        return f"s3://{self.bucket}/{key}"
