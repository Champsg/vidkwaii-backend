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
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
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

MEDIA_DIR = Path(settings.media_dir)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

# Registered produced files (merged videos / extracted audio): file_id -> meta
_media_files: dict[str, dict[str, Any]] = {}


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
    fps: Optional[float] = None
    vcodec: Optional[str] = None
    acodec: Optional[str] = None
    abr: Optional[float] = None
    tbr: Optional[float] = None
    filesize: Optional[float] = None
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
    duration: Optional[float] = None
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


class MergeRequest(BaseModel):
    url: str = Field(..., min_length=4, max_length=4096)
    quality: Optional[str] = Field(
        default=None,
        description="best | 2160p | 1440p | 1080p | 720p | 480p | 360p",
    )


class AudioRequest(BaseModel):
    url: str = Field(..., min_length=4, max_length=4096)
    format: str = Field(default="m4a", pattern="^(mp3|m4a)$")


class MediaFileInfo(BaseModel):
    url: str
    filename: str
    size: Optional[int] = None


class MergeResponse(BaseModel):
    ok: bool = True
    file: MediaFileInfo


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


_QUALITY_SELECTORS = {
    "best": "bestvideo+bestaudio/best",
    "2160p": "bestvideo[height<=2160]+bestaudio/best[height<=2160]/best",
    "1440p": "bestvideo[height<=1440]+bestaudio/best[height<=1440]/best",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
    "360p": "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
}

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


def _validate_url(url: str) -> None:
    lowered = url.strip().lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        raise HTTPException(
            status_code=422,
            detail=ErrorDetail(
                code="INVALID_URL",
                message="Only http(s) URLs are supported.",
                suggestion="Paste a full link starting with http:// or https://.",
            ).model_dump(),
        )


def _normalize_target_url(url: str) -> str:
    target = url.strip()
    # x.com -> twitter.com for the fast syndication API extractor.
    if "x.com" in target:
        target = target.replace("x.com", "twitter.com")
    return target


def _build_ydl_opts() -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "playlist_items": "1",
        "socket_timeout": 30,
        "retries": 3,
        "nocheckcertificate": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios", "mweb", "web"],
                "player_skip": ["js", "configs", "webpage"],
            },
            "twitter": {"api": ["syndication"]},
        },
        "http_headers": _BROWSER_HEADERS,
    }


def _sweep_media_files() -> None:
    """Delete produced files older than the TTL so the temp dir stays small."""
    cutoff = time.monotonic() - settings.media_ttl_seconds
    expired = [fid for fid, meta in _media_files.items() if meta["created"] < cutoff]
    for fid in expired:
        meta = _media_files.pop(fid, None)
        if meta:
            try:
                Path(meta["path"]).unlink(missing_ok=True)
            except OSError:
                pass


def _produce_media_file(
    url: str,
    kind: str,
    quality: Optional[str] = None,
    audio_format: str = "m4a",
) -> MediaFileInfo:
    """Download + (optionally) merge media on the server with yt-dlp + ffmpeg."""
    _sweep_media_files()
    target = _normalize_target_url(url)
    work_id = uuid.uuid4().hex
    opts = _build_ydl_opts()
    opts["outtmpl"] = str(MEDIA_DIR / f"{work_id}.%(ext)s")
    if kind == "merge":
        selector = _QUALITY_SELECTORS.get(quality or "", _QUALITY_SELECTORS["best"])
        opts.update(
            {
                "format": selector,
                "merge_output_format": "mp4",
            }
        )
    else:
        opts.update(
            {
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": audio_format,
                        "preferredquality": "192",
                    }
                ],
            }
        )

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=True)
    except yt_dlp.utils.DownloadError as exc:
        raise ExtractorFailure(
            "MERGE_FAILED",
            f"Could not download media: {exc}",
            "The site may block datacenter IPs or the media may be unavailable.",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Server media production failed")
        raise ExtractorFailure(
            "MERGE_FAILED",
            f"Unexpected error while producing media: {exc}",
            None,
        ) from exc

    candidates = sorted(
        MEDIA_DIR.glob(f"{work_id}.*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise ExtractorFailure(
            "MERGE_FAILED",
            "yt-dlp finished but produced no output file.",
            "Try again or pick a different source.",
        )
    path = candidates[0]
    file_id = uuid.uuid4().hex
    filename = f"{_slugify(info.get('title') or path.stem)}{path.suffix}"
    _media_files[file_id] = {"path": str(path), "created": time.monotonic()}
    return MediaFileInfo(
        url=f"/api/v1/files/{file_id}",
        filename=filename,
        size=path.stat().st_size,
    )


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
    target_url = request.url.strip()
    clean_path = target_url.split("?")[0].lower()
    
    # 1. Instant handling for direct media URLs (MP4, M3U8, MP3, etc.)
    direct_exts = (".mp4", ".m3u8", ".webm", ".mov", ".mp3", ".m4a", ".aac")
    if any(clean_path.endswith(ext) for ext in direct_exts):
        raw_name = target_url.split("?")[0].split("/")[-1] or "media_file"
        ext = raw_name.split(".")[-1] if "." in raw_name else "mp4"
        is_audio = ext in ("mp3", "m4a", "aac")
        is_hls = ext == "m3u8" or "m3u8" in target_url.lower()
        return {
            "id": "direct",
            "title": raw_name,
            "url": target_url,
            "extractor": "Direct Media",
            "formats": [
                {
                    "format_id": "direct",
                    "url": target_url,
                    "ext": ext,
                    "protocol": "https" if target_url.startswith("https") else "http",
                    "resolution": "Direct Audio Stream" if is_audio else "Direct Video Stream",
                    "has_video": not is_audio,
                    "has_audio": True,
                    "is_hls": is_hls,
                }
            ],
        }

    # 2. X.com -> twitter.com normalization for fast syndication extraction
    if "x.com" in target_url:
        target_url = target_url.replace("x.com", "twitter.com")

    opts = _build_ydl_opts()
    opts["skip_download"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target_url, download=False)
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
    _validate_url(request.url)
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
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "media_files": len(_media_files),
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


@app.post(
    "/api/v1/merge",
    response_model=MergeResponse,
    responses={422: {"model": ErrorResponse}, 504: {"model": ErrorResponse}},
)
async def merge_endpoint(request: MergeRequest) -> MergeResponse:
    """Download video+audio streams and merge them server-side into one .mp4."""
    _validate_url(request.url)
    try:
        file_info = await asyncio.wait_for(
            asyncio.to_thread(
                _produce_media_file,
                request.url,
                "merge",
                quality=request.quality,
            ),
            timeout=settings.max_merge_seconds,
        )
    except ExtractorFailure as exc:
        raise HTTPException(
            status_code=422,
            detail=ErrorDetail(
                code=exc.code,
                message=exc.message,
                suggestion=exc.suggestion,
            ).model_dump(),
        ) from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=ErrorDetail(
                code="MERGE_TIMEOUT",
                message="Merging took too long and was cancelled.",
                suggestion="Try a lower quality or a shorter video.",
            ).model_dump(),
        ) from exc
    return MergeResponse(file=file_info)


@app.post(
    "/api/v1/audio",
    response_model=MergeResponse,
    responses={422: {"model": ErrorResponse}, 504: {"model": ErrorResponse}},
)
async def audio_endpoint(request: AudioRequest) -> MergeResponse:
    """Download the best audio track and transcode it to .m4a or .mp3."""
    _validate_url(request.url)
    try:
        file_info = await asyncio.wait_for(
            asyncio.to_thread(
                _produce_media_file,
                request.url,
                "audio",
                audio_format=request.format,
            ),
            timeout=settings.max_merge_seconds,
        )
    except ExtractorFailure as exc:
        raise HTTPException(
            status_code=422,
            detail=ErrorDetail(
                code=exc.code,
                message=exc.message,
                suggestion=exc.suggestion,
            ).model_dump(),
        ) from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=ErrorDetail(
                code="MERGE_TIMEOUT",
                message="Audio extraction took too long and was cancelled.",
                suggestion="Try again in a few minutes.",
            ).model_dump(),
        ) from exc
    return MergeResponse(file=file_info)


@app.get("/api/v1/files/{file_id}")
async def serve_media_file(file_id: str) -> FileResponse:
    """Stream a produced file. FileResponse supports HTTP Range requests, so
    the mobile client can chunk-download it in parallel."""
    meta = _media_files.get(file_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="File not found or expired.")
    path = Path(meta["path"])
    if not path.exists():
        _media_files.pop(file_id, None)
        raise HTTPException(status_code=404, detail="File not found or expired.")
    return FileResponse(path, filename=path.name)
