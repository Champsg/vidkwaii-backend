"""Media Hub — FastAPI extractor backend.

Exposes ``/api/v1/extract`` which resolves a URL with yt-dlp and normalizes
the raw extractor output into a clean, client-friendly payload: formats,
subtitles, thumbnails, direct/HLS detection and a suggested filename.
Results are cached in Redis with an automatic in-memory fallback.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import Any, Optional

import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.cache import MediaCache
from app.config import settings

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("media_hub.api")

app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description=(
        "Universal media extraction & normalization service for the Media Hub "
        "app. Resolves pages, direct files and HLS playlists into normalized "
        "formats."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

cache = MediaCache(
    redis_url=settings.redis_url,
    ttl_seconds=settings.cache_ttl_seconds,
)


# ---------------------------------------------------------------- schemas


class ExtractRequest(BaseModel):
    url: str = Field(
        ...,
        min_length=4,
        max_length=4096,
        description="URL of the media (page, direct file or .m3u8 playlist).",
    )
    audio_only: bool = Field(
        default=False,
        description="Hint that the client only needs an audio track.",
    )
    preferred_quality: Optional[str] = Field(
        default=None,
        description="best | audio | 2160p | 1440p | 1080p | 720p | 480p | 360p",
    )
    format_id: Optional[str] = Field(default=None, description="Exact yt-dlp format id to recommend.")
    max_filesize_mb: Optional[int] = Field(default=None, ge=1, le=4096)


class FormatInfo(BaseModel):
    format_id: str
    ext: str
    url: str
    protocol: str = ""
    resolution: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[int] = None
    vcodec: Optional[str] = None
    acodec: Optional[str] = None
    abr: Optional[float] = None
    tbr: Optional[float] = None
    filesize: Optional[int] = None
    format_note: Optional[str] = None
    has_video: bool = False
    has_audio: bool = False
    is_hls: bool = False
    quality_label: Optional[str] = None


class SubtitleInfo(BaseModel):
    lang: str
    name: str
    ext: str
    url: str


class ThumbnailInfo(BaseModel):
    url: str
    width: Optional[int] = None
    height: Optional[int] = None
    resolution: Optional[str] = None


class MediaInfo(BaseModel):
    id: str
    url: str
    title: str
    description: Optional[str] = None
    uploader: Optional[str] = None
    upload_date: Optional[str] = None
    duration: Optional[int] = None
    duration_label: Optional[str] = None
    view_count: Optional[int] = None
    is_live: bool = False
    is_hls: bool = False
    is_direct: bool = False
    thumbnail: Optional[str] = None
    thumbnails: list[ThumbnailInfo] = Field(default_factory=list)
    formats: list[FormatInfo] = Field(default_factory=list)
    subtitles: list[SubtitleInfo] = Field(default_factory=list)
    suggested_filename: Optional[str] = None
    recommended_format_id: Optional[str] = None
    recommendation_reason: Optional[str] = None
    webpage_url: Optional[str] = None
    webpage_title: Optional[str] = None
    extractor: Optional[str] = None


class ExtractResponse(BaseModel):
    ok: bool = True
    cached: bool = False
    data: MediaInfo


class ErrorDetail(BaseModel):
    code: str
    message: str
    suggestion: Optional[str] = None


class ErrorResponse(BaseModel):
    ok: bool = False
    error: ErrorDetail


# ---------------------------------------------------------------- errors


class ExtractorFailure(Exception):
    """Domain error carrying a machine-readable code and a user hint."""

    def __init__(self, code: str, message: str, suggestion: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestion = suggestion


# ---------------------------------------------------------------- helpers


def _slugify(text: Optional[str], fallback: str = "media") -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", " ", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()[:120].strip()
    return cleaned or fallback


def _duration_label(seconds: Optional[int]) -> Optional[str]:
    if not seconds:
        return None
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _quality_label(fmt: dict[str, Any]) -> Optional[str]:
    width, height = fmt.get("width"), fmt.get("height")
    if height:
        return f"{height}p" + (f" · {width}x{height}" if width else "")
    abr = fmt.get("abr") or fmt.get("tbr")
    if abr:
        return f"{abr:.0f} kbps"
    return fmt.get("format_note") or fmt.get("format_id")


def _recommend(
    formats: list[FormatInfo],
    request: ExtractRequest,
) -> tuple[Optional[str], Optional[str]]:
    """Pick the best format id for the client, plus a human-readable reason."""

    if not formats:
        return None, None

    max_bytes = request.max_filesize_mb * 1024 * 1024 if request.max_filesize_mb else None
    pool = formats
    if max_bytes is not None:
        known = [f for f in pool if f.filesize is not None]
        if known:
            pool = [f for f in pool if f.filesize is None or f.filesize <= max_bytes]
            if not pool:
                return None, f"all formats exceed {request.max_filesize_mb} MB"

    if request.format_id:
        for fmt in pool:
            if fmt.format_id == request.format_id:
                return fmt.format_id, "exact format requested"

    if request.audio_only:
        audio_only = [f for f in pool if f.has_audio and not f.has_video]
        if audio_only:
            best = max(audio_only, key=lambda f: f.abr or 0)
            return best.format_id, "best audio-only format"
        best = max(pool, key=lambda f: f.abr or 0)
        return best.format_id, "best available audio track"

    height_map = {"2160p": 2160, "1440p": 1440, "1080p": 1080, "720p": 720, "480p": 480, "360p": 360}
    target_height = height_map.get(request.preferred_quality or "")

    if request.preferred_quality == "best" or target_height is None:
        combined = [f for f in pool if f.has_video and f.has_audio]
        candidates = combined or [f for f in pool if f.has_video]
        if not candidates:
            return None, "no playable video format found"
        best = max(candidates, key=lambda f: (f.height or 0, f.tbr or 0))
        return best.format_id, "best combined format" if combined else "best video-only format"

    candidates = [f for f in pool if f.has_video and (f.height or 0) <= target_height]
    if not candidates:
        return None, f"no format at or below {request.preferred_quality}"
    combined = [f for f in candidates if f.has_audio]
    best = max(combined or candidates, key=lambda f: (f.height or 0, f.tbr or 0))
    reason = f"best format at or below {request.preferred_quality}"
    if not combined:
        reason = "video-only format at requested quality (audio muxed separately)"
    return best.format_id, reason


def normalize(info: dict[str, Any], request: ExtractRequest) -> MediaInfo:
    """Map raw yt-dlp output onto the stable MediaInfo contract."""

    raw_formats = info.get("formats") or []
    formats: list[FormatInfo] = []
    any_hls = False
    any_direct = False

    for fmt in raw_formats:
        url = fmt.get("url")
        if not url:
            continue
        protocol = (fmt.get("protocol") or "").lower()
        manifest_url = fmt.get("manifest_url") or ""
        is_hls = "m3u8" in protocol or "m3u8" in (url or "").lower() or "m3u8" in manifest_url.lower()
        has_video = bool(fmt.get("vcodec") and fmt.get("vcodec") != "none")
        has_audio = bool(fmt.get("acodec") and fmt.get("acodec") != "none")
        if is_hls:
            any_hls = True
        if protocol in {"http", "https"} and not is_hls and has_video:
            any_direct = True
        formats.append(
            FormatInfo(
                format_id=str(fmt.get("format_id") or ""),
                ext=fmt.get("ext") or "unknown",
                url=url,
                protocol=protocol,
                resolution=fmt.get("resolution"),
                width=fmt.get("width"),
                height=fmt.get("height"),
                fps=fmt.get("fps"),
                vcodec=fmt.get("vcodec"),
                acodec=fmt.get("acodec"),
                abr=fmt.get("abr"),
                tbr=fmt.get("tbr"),
                filesize=fmt.get("filesize") or fmt.get("filesize_approx"),
                format_note=fmt.get("format_note"),
                has_video=has_video,
                has_audio=has_audio,
                is_hls=is_hls,
                quality_label=_quality_label(fmt),
            )
        )

    subtitles: list[SubtitleInfo] = []
    for lang, tracks in (info.get("subtitles") or {}).items():
        tracks = tracks or []
        if not tracks:
            continue
        chosen = max(tracks, key=lambda t: {"m3u8": 3, "vtt": 2, "srt": 1}.get((t.get("ext") or ""), 0))
        subtitles.append(
            SubtitleInfo(
                lang=lang,
                name=chosen.get("name") or lang,
                ext=chosen.get("ext") or "vtt",
                url=chosen.get("url") or "",
            )
        )
    if not subtitles:
        for lang, tracks in (info.get("automatic_captions") or {}).items():
            tracks = tracks or []
            if not tracks:
                continue
            chosen = max(tracks, key=lambda t: {"m3u8": 3, "vtt": 2, "srt": 1}.get((t.get("ext") or ""), 0))
            subtitles.append(
                SubtitleInfo(
                    lang=lang,
                    name=chosen.get("name") or f"{lang} (auto)",
                    ext=chosen.get("ext") or "vtt",
                    url=chosen.get("url") or "",
                )
            )

    thumbnails = [
        ThumbnailInfo(
            url=t.get("url") or "",
            width=t.get("width"),
            height=t.get("height"),
            resolution=(
                f"{t.get('width')}x{t.get('height')}"
                if t.get("width") and t.get("height")
                else None
            ),
        )
        for t in (info.get("thumbnails") or [])
        if t.get("url")
    ]
    thumbnail = info.get("thumbnail")
    if not thumbnail and thumbnails:
        by_size = sorted(thumbnails, key=lambda t: (t.width or 0) * (t.height or 0))
        thumbnail = by_size[-1].url

    recommended_id, reason = _recommend(formats, request)
    recommended = next((f for f in formats if f.format_id == recommended_id), formats[0] if formats else None)
    ext = recommended.ext if recommended else "mp4"
    suggested = f"{_slugify(info.get('title'))}.{ext}" if formats else f"{_slugify(info.get('title'))}.mp4"

    return MediaInfo(
        id=str(info.get("id") or ""),
        url=request.url,
        title=info.get("title") or "Untitled media",
        description=info.get("description"),
        uploader=info.get("uploader") or info.get("channel") or info.get("creator"),
        upload_date=info.get("upload_date"),
        duration=info.get("duration"),
        duration_label=_duration_label(info.get("duration")),
        view_count=info.get("view_count"),
        is_live=bool(info.get("is_live")),
        is_hls=any_hls,
        is_direct=any_direct,
        thumbnail=thumbnail,
        thumbnails=thumbnails,
        formats=formats,
        subtitles=subtitles,
        suggested_filename=suggested,
        recommended_format_id=recommended_id,
        recommendation_reason=reason,
        webpage_url=info.get("webpage_url"),
        webpage_title=info.get("webpage_url") and info.get("title"),
        extractor=info.get("extractor_key") or info.get("extractor"),
    )


def _run_ytdlp(request: ExtractRequest) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "playlist_items": "1",
        "extract_flat": False,
        "socket_timeout": 30,
        "retries": 2,
        "nocheckcertificate": False,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(request.url, download=False)
    except yt_dlp.utils.UnsupportedError as exc:
        raise ExtractorFailure(
            "UNSUPPORTED_URL",
            "This link is not supported by the extractor.",
            "Try a direct MP4 or .m3u8 URL instead.",
        ) from exc
    except yt_dlp.utils.RegexNotFoundError as exc:
        raise ExtractorFailure(
            "EXTRACTION_FAILED",
            "No extractor matched this URL.",
            "Double-check the link, or paste a direct file URL.",
        ) from exc
    except yt_dlp.utils.ExtractorError as exc:
        raise ExtractorFailure(
            "EXTRACTOR_ERROR",
            f"Extractor failed: {exc}",
            "The site may have changed its layout or blocked automated access.",
        ) from exc
    except yt_dlp.utils.DownloadError as exc:
        raise ExtractorFailure(
            "DOWNLOAD_ERROR",
            f"Could not resolve media: {exc}",
            "Retry in a few minutes, or try a direct file URL.",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected extraction failure")
        raise ExtractorFailure(
            "UNKNOWN_ERROR",
            f"Unexpected error while extracting: {exc}",
            None,
        ) from exc

    if not info:
        raise ExtractorFailure("EMPTY_RESULT", "Extractor returned no metadata.", None)
    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        if not entries:
            raise ExtractorFailure("EMPTY_RESULT", "Playlist contained no playable entries.", None)
        info = entries[0]
    return info


async def _extract(request: ExtractRequest) -> tuple[MediaInfo, bool]:
    cache_key = hashlib.sha256(
        f"{request.url}|{request.audio_only}|{request.preferred_quality}|{request.format_id}".encode()
    ).hexdigest()

    cached = await cache.get(cache_key)
    if cached:
        try:
            return MediaInfo.model_validate_json(cached), True
        except Exception:
            logger.warning("Dropped stale cache entry for key=%s", cache_key[:8])

    raw = await asyncio.wait_for(
        asyncio.to_thread(_run_ytdlp, request),
        timeout=settings.max_extract_seconds,
    )
    normalized = normalize(raw, request)
    await cache.set(cache_key, normalized.model_dump_json())
    return normalized, False


async def _handle(request: ExtractRequest) -> ExtractResponse:
    lowered = request.url.strip().lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        raise HTTPException(
            status_code=422,
            detail=ErrorDetail(
                code="INVALID_URL",
                message="Only http(s) URLs are supported.",
                suggestion="Paste a full link starting with http:// or https://.",
            ).model_dump(),
        )
    try:
        info, cached = await _extract(request)
    except ExtractorFailure as exc:
        raise HTTPException(
            status_code=422,
            detail=ErrorDetail(code=exc.code, message=exc.message, suggestion=exc.suggestion).model_dump(),
        ) from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=ErrorDetail(
                code="TIMEOUT",
                message="Extraction took too long and was cancelled.",
                suggestion="Try a direct MP4 or .m3u8 URL.",
            ).model_dump(),
        ) from exc
    return ExtractResponse(ok=True, cached=cached, data=info)


# ---------------------------------------------------------------- routes


@app.get("/health")
async def health() -> dict[str, Any]:
    redis_ok = await cache.ensure_redis()
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": app.version,
        "redis": redis_ok,
    }


@app.get(
    "/api/v1/extract",
    response_model=ExtractResponse,
    responses={422: {"model": ErrorResponse}, 504: {"model": ErrorResponse}},
)
async def extract_get(
    url: str = Query(..., min_length=4, max_length=4096),
    audio_only: bool = Query(default=False),
    preferred_quality: Optional[str] = Query(default=None),
) -> ExtractResponse:
    return await _handle(
        ExtractRequest(
            url=url,
            audio_only=audio_only,
            preferred_quality=preferred_quality,
        )
    )


@app.post(
    "/api/v1/extract",
    response_model=ExtractResponse,
    responses={422: {"model": ErrorResponse}, 504: {"model": ErrorResponse}},
)
async def extract_post(request: ExtractRequest) -> ExtractResponse:
    return await _handle(request)
