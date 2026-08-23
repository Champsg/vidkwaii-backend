# Media Hub — Extractor Backend

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

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```
