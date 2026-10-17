"""AC-API-20: GET /media/{key} streams from S3 with the right content-type; 404 if missing; no path traversal."""
from __future__ import annotations

import pytest

from inventory_api.storage import InvalidKey, ObjectNotFound, uri_to_url, validate_key
from tests.conftest import BUCKET, JPEG_BYTES, PNG_BYTES


def test_ac_api_20_streams_object_with_content_type(client, s3):
    s3.put_object(Bucket=BUCKET, Key="frames/cam-lab1-fwd/0001.jpg", Body=JPEG_BYTES, ContentType="image/jpeg")
    r = client.get("/media/frames/cam-lab1-fwd/0001.jpg")
    assert r.status_code == 200
    assert r.content == JPEG_BYTES
    assert r.headers["content-type"].startswith("image/jpeg")


def test_ac_api_20_png_content_type(client, s3):
    s3.put_object(Bucket=BUCKET, Key="crops/a.png", Body=PNG_BYTES, ContentType="image/png")
    r = client.get("/media/crops/a.png")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")


def test_ac_api_20_missing_object_is_404(client, s3):
    r = client.get("/media/frames/does-not-exist.jpg")
    assert r.status_code == 404


@pytest.mark.parametrize("path", [
    "/media/..%2F..%2Fetc%2Fpasswd",
    "/media/frames%2F..%2F..%2Fsecret.txt",
    "/media/%2E%2E/secret.txt",
    "/media/frames/%2e%2e/%2e%2e/secret.txt",
    "/media/%2Fetc%2Fpasswd",
    "/media/frames%5C..%5Csecret.txt",
])
def test_ac_api_20_path_traversal_rejected(client, s3, path):
    # Even if an object literally named with '..' segments exists, it must not be served.
    for key in ("../secret.txt", "frames/../../secret.txt", "../../etc/passwd", "../etc/passwd", "secret.txt",
                "frames/../secret.txt", "/etc/passwd", "frames\\..\\secret.txt"):
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"TOP SECRET", ContentType="text/plain")
    r = client.get(path)
    assert r.status_code in (400, 404, 422), (path, r.status_code)
    assert b"TOP SECRET" not in r.content


@pytest.mark.parametrize("key", ["../a.jpg", "a/../../b.jpg", "/abs.jpg", "a\\..\\b.jpg", "", "a/./b.jpg",
                                 "a//b.jpg"])
def test_ac_api_20_validate_key_rejects_unsafe_keys(key):
    with pytest.raises(InvalidKey):
        validate_key(key)


@pytest.mark.parametrize("key", ["frames/cam-lab1-fwd/0001.jpg", "crops/a.png", "x.jpg"])
def test_ac_api_20_validate_key_accepts_normal_keys(key):
    assert validate_key(key) == key


def test_ac_api_20_s3_store_roundtrip(store, s3):
    uri = store.put("crops/b.png", PNG_BYTES, "image/png")
    assert uri == f"s3://{BUCKET}/crops/b.png"
    obj = store.get("crops/b.png")
    assert obj.data == PNG_BYTES
    assert obj.content_type == "image/png"


def test_ac_api_20_s3_store_missing_raises_not_found(store):
    with pytest.raises(ObjectNotFound):
        store.get("nope.png")


def test_ac_api_20_s3_store_rejects_traversal(store):
    with pytest.raises(InvalidKey):
        store.get("../x.png")


def test_ac_api_20_uri_to_url_mapping():
    assert uri_to_url(f"s3://{BUCKET}/frames/a.jpg", BUCKET) == "/media/frames/a.jpg"
    assert uri_to_url(None, BUCKET) is None
