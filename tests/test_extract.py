"""Unit tests for the extract endpoint using a stubbed yt-dlp."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient
import yt_dlp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as app_module  # noqa: E402


FIXTURE = {
    "id": "abc123",
    "title": "  Sample  Video  ",
    "description": "A sample description.",
    "uploader": "Test Channel",
    "upload_date": "20250102",
    "duration": 3661,
    "view_count": 1234,
    "webpage_url": "https://example.com/watch?v=abc123",
    "extractor_key": "Generic",
    "thumbnail": "https://example.com/thumb.jpg",
    "formats": [
        {
            "format_id": "1080",
            "ext": "mp4",
            "url": "https://cdn.example.com/video-1080.mp4",
            "protocol": "https",
            "width": 1920,
            "height": 1080,
            "vcodec": "avc1",
            "acodec": "mp4a",
            "tbr": 3500.0,
            "filesize": 52428800,
            "format_note": "1080p",
        },
        {
            "format_id": "audio",
            "ext": "m4a",
            "url": "https://cdn.example.com/audio.m4a",
            "protocol": "https",
            "vcodec": "none",
            "acodec": "mp4a",
            "abr": 128.0,
        },
        {
            "format_id": "hls-720",
            "ext": "mp4",
            "url": "https://cdn.example.com/hls/master.m3u8",
            "protocol": "m3u8_native",
            "manifest_url": "https://cdn.example.com/hls/master.m3u8",
            "width": 1280,
            "height": 720,
            "vcodec": "avc1",
            "acodec": "none",
        },
    ],
    "subtitles": {
        "en": [{"ext": "vtt", "url": "https://example.com/en.vtt", "name": "English"}],
    },
}


class FakeYoutubeDL:
    def __init__(self, opts: dict) -> None:
        self.opts = opts
        self.last_url: str | None = None

    def extract_info(self, url: str, download: bool = False) -> dict:
        self.last_url = url
        assert download is False
        if "unsupported" in url:
            raise yt_dlp.utils.UnsupportedError(url)
        return dict(FIXTURE)

    def __enter__(self) -> "FakeYoutubeDL":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def close(self) -> None:
        pass


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr(app_module.yt_dlp, "YoutubeDL", lambda opts: FakeYoutubeDL(opts))
    app_module.cache = app_module.MediaCache(
        redis_url="redis://127.0.0.1:1/0",
        ttl_seconds=60,
    )
    with TestClient(app_module.app) as test_client:
        yield test_client


def test_extract_normalizes_metadata(client: TestClient) -> None:
    response = client.post(
        "/api/v1/extract",
        json={"url": "https://www.instagram.com/reel/abc123/", "preferred_quality": "1080p"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    data = body["data"]

    assert data["id"] == "abc123"
    assert data["title"] == "Sample  Video"
    assert data["duration"] == 3661
    assert data["duration_label"] == "1:01:01"
    assert data["is_hls"] is True
    assert data["is_direct"] is True
    assert data["recommended_format_id"] == "1080"
    assert data["suggested_filename"] == "Sample  Video.mp4"
    assert len(data["formats"]) == 3
    assert len(data["subtitles"]) == 1
    assert data["subtitles"][0]["lang"] == "en"


def test_extract_audio_only_prefers_audio(client: TestClient) -> None:
    response = client.post(
        "/api/v1/extract",
        json={"url": "https://www.instagram.com/reel/abc123/", "audio_only": True},
    )
    data = response.json()["data"]
    assert data["recommended_format_id"] == "audio"


def test_extract_hls_detection(client: TestClient) -> None:
    response = client.post("/api/v1/extract", json={"url": "https://www.instagram.com/reel/abc123/"})
    hls_formats = [f for f in response.json()["data"]["formats"] if f["is_hls"]]
    assert hls_formats and hls_formats[0]["protocol"] == "m3u8_native"


def test_unsupported_url_returns_422(client: TestClient) -> None:
    response = client.post("/api/v1/extract", json={"url": "https://www.instagram.com/unsupported"})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "UNSUPPORTED_URL"


def test_invalid_url_scheme_returns_422(client: TestClient) -> None:
    response = client.post("/api/v1/extract", json={"url": "ftp://example.com/file.mp4"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_URL"


def test_direct_media_url_bypasses_extractor(client: TestClient, monkeypatch) -> None:
    """Direct .mp4/.m3u8 URLs are resolved without scraping."""
    called = {"value": False}

    def fake_ydl(_opts):
        called["value"] = True
        raise AssertionError("yt-dlp must not be invoked for direct URLs")

    monkeypatch.setattr(app_module.yt_dlp, "YoutubeDL", fake_ydl)
    response = client.post(
        "/api/v1/extract",
        json={"url": "https://scontent.cdninstagram.com/clip.mp4?token=abc"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["title"] == "clip.mp4"
    assert data["is_direct"] is True
    assert data["formats"][0]["url"].startswith("https://scontent.cdninstagram.com/clip.mp4")
    assert called["value"] is False


def test_non_instagram_url_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/extract",
        json={"url": "https://www.youtube.com/watch?v=xyz123"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INSTAGRAM_ONLY"


def test_instagram_short_link_is_accepted(client: TestClient) -> None:
    response = client.post("/api/v1/extract", json={"url": "https://instagr.am/p/abc123/"})
    assert response.status_code == 200
    assert response.json()["data"]["title"] == "Sample  Video"


def test_merge_endpoint_returns_file_info(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        app_module,
        "_produce_media_file",
        lambda url, kind, quality=None, audio_format="m4a": app_module.MediaFileInfo(
            url="/api/v1/files/abc",
            filename="merged.mp4",
            size=12345,
        ),
    )
    response = client.post(
        "/api/v1/merge",
        json={"url": "https://www.instagram.com/reel/abc123/", "quality": "1080p"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["file"]["filename"] == "merged.mp4"
    assert body["file"]["url"] == "/api/v1/files/abc"


def test_audio_endpoint_rejects_bad_format(client: TestClient) -> None:
    response = client.post(
        "/api/v1/audio",
        json={"url": "https://www.instagram.com/reel/abc123/", "format": "wav"},
    )
    assert response.status_code == 422


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
