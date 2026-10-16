# VidKwaii — Extractor Backend

FastAPI service that normalizes yt-dlp output into a stable payload for the
Flutter client.

## Run locally

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Redis is optional — the API falls back to an in-memory cache when Redis is
unreachable. To enable caching, run `docker compose up -d redis` or set
`REDIS_URL`.

## Endpoints

| Method | Path               | Description                              |
| ------ | ------------------ | ---------------------------------------- |
| POST   | `/api/v1/extract`  | Extract + normalize metadata from a URL  |
| GET    | `/api/v1/extract?url=...` | Same, query-string variant        |
| POST   | `/api/v1/merge`    | Server-side download + FFmpeg merge of separate video/audio streams into one `.mp4` (returns a range-servable URL) |
| POST   | `/api/v1/audio`    | Extract the best audio track as `.m4a` / `.mp3` (range-servable URL) |
| GET    | `/api/v1/files/{file_id}` | Stream a produced file (supports HTTP Range for parallel chunking) |
| GET    | `/health`          | Health check incl. Redis status          |

Interactive docs: <http://localhost:8000/docs>

## Example

```bash
curl -X POST http://localhost:8000/api/v1/extract \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ","preferred_quality":"1080p"}'
```

The response includes normalized formats (with direct/HLS flags), subtitles,
thumbnails, a suggested filename, and a server-side format recommendation.

Direct media URLs (`.mp4`, `.m3u8`, `.mp3`, `.mov`, `.webm`, …) bypass
yt-dlp entirely and are resolved instantly. YouTube extraction uses the
`android/ios/mweb/web` player clients, `x.com` links are normalized to
`twitter.com` (syndication API), and a realistic browser User-Agent is sent.

Produced files live in `MEDIA_DIR` and are garbage-collected after
`MEDIA_TTL_SECONDS` (default 30 minutes).

An automatic background keep-alive task runs every 10 minutes (`KEEP_ALIVE_INTERVAL_SECONDS=600`) to send pings to `RENDER_EXTERNAL_URL` (or localhost `/health`), preventing free Render web services from spinning down due to inactivity.


## Tests


```bash
pip install -r requirements-dev.txt
pytest
```
