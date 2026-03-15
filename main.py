"""
Railway FastAPI Backend — Universal Video Extractor
Uses yt-dlp (Python library) to extract video/audio download links.
NO files are stored on disk; this is a pure extractor/metadata proxy.

Pydantic is intentionally NOT used — it requires Rust to build on Python 3.13+
and causes Railway build failures. We use plain dicts instead.
"""

import os
import re
import logging
from typing import Optional, List, Dict, Any

import yt_dlp
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Universal Video Extractor",
    description="Extracts video/audio download links using yt-dlp. No files stored.",
    version="1.0.0",
)

# ── CORS ──────────────────────────────────────────────────────────────────────
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Optional API secret guard ─────────────────────────────────────────────────
API_SECRET = os.getenv("API_SECRET", "")


def check_secret(x_api_secret: Optional[str] = None) -> None:
    if API_SECRET and x_api_secret != API_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden: invalid API secret.")


# ─────────────────────────────────────────────────────────────────────────────
# yt-dlp helpers
# ─────────────────────────────────────────────────────────────────────────────
def build_ydl_opts(url: str) -> dict:
    is_tiktok = bool(re.search(r'tiktok\.com', url, re.I))

    opts: Dict[str, Any] = {
        "quiet":          True,
        "no_warnings":    True,
        "extract_flat":   False,
        "skip_download":  True,
        "noplaylist":     True,
        "geo_bypass":     True,
        "socket_timeout": 30,
    }

    # TikTok: prefer non-watermarked stream
    if is_tiktok:
        opts["format"] = (
            "bestvideo[format_id!*=watermark]+bestaudio[format_id!*=watermark]"
            "/best[format_id!*=watermark]"
            "/bestvideo+bestaudio/best"
        )

    proxy = os.getenv("YTDLP_PROXY", "")
    if proxy:
        opts["proxy"] = proxy

    cookies_file = os.getenv("YTDLP_COOKIES_FILE", "")
    if cookies_file and os.path.isfile(cookies_file):
        opts["cookiefile"] = cookies_file

    opts["http_headers"] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }

    return opts


def extract_info(url: str) -> dict:
    opts = build_ydl_opts(url)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return ydl.sanitize_info(info)


def parse_formats(raw_formats: list) -> List[dict]:
    results = []

    for f in raw_formats:
        fmt_url = f.get("url") or f.get("manifest_url") or ""
        if not fmt_url:
            continue
        if fmt_url.endswith(".m3u8") or "manifest" in fmt_url.lower():
            continue

        width  = f.get("width")
        height = f.get("height")
        resolution = f.get("resolution") or (
            f"{width}x{height}" if width and height else None
        )

        results.append({
            "format_id":   f.get("format_id", ""),
            "ext":         f.get("ext", ""),
            "resolution":  resolution,
            "format_note": f.get("format_note"),
            "filesize":    f.get("filesize") or f.get("filesize_approx"),
            "url":         fmt_url,
            "vcodec":      f.get("vcodec"),
            "acodec":      f.get("acodec"),
            "tbr":         f.get("tbr"),
            "abr":         f.get("abr"),
            "fps":         f.get("fps"),
        })

    # Deduplicate by (resolution, ext) keeping largest filesize
    seen: Dict[str, dict] = {}
    for item in results:
        key = f"{item['resolution']}|{item['ext']}"
        existing = seen.get(key)
        if not existing:
            seen[key] = item
        else:
            if (item["filesize"] or 0) > (existing["filesize"] or 0):
                seen[key] = item

    def sort_key(fi: dict):
        is_audio_only = (not fi["vcodec"] or fi["vcodec"] == "none")
        height = 0
        if fi["resolution"]:
            m = re.search(r"(\d+)x(\d+)", fi["resolution"])
            if m:
                height = int(m.group(2))
        return (0 if not is_audio_only else 1, -height)

    return sorted(seen.values(), key=sort_key)


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
async def root():
    return {"status": "ok", "message": "Video Extractor API is running."}


@app.post("/extract")
async def extract_endpoint(
    body: dict,
    x_api_secret: Optional[str] = Header(default=None),
):
    check_secret(x_api_secret)

    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required.")

    logger.info(f"Extracting: {url}")

    try:
        info = extract_info(url)

    except yt_dlp.utils.DownloadError as e:
        err_str = str(e)
        logger.warning(f"yt-dlp DownloadError: {err_str}")

        if "Unsupported URL" in err_str or "No video formats" in err_str:
            raise HTTPException(status_code=422, detail="This URL is not supported.")
        if "Private video" in err_str or "login required" in err_str.lower():
            raise HTTPException(status_code=422, detail="This video is private or requires login.")
        if "Video unavailable" in err_str:
            raise HTTPException(status_code=422, detail="This video is unavailable or has been removed.")
        raise HTTPException(status_code=422, detail=f"Extraction failed: {err_str[:200]}")

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred. Please try again.")

    raw_formats: list = info.get("formats") or []
    if not raw_formats and info.get("url"):
        raw_formats = [info]

    formats = parse_formats(raw_formats)

    if not formats:
        raise HTTPException(status_code=422, detail="No downloadable formats were found.")

    return {
        "title":       info.get("title", "Untitled"),
        "thumbnail":   info.get("thumbnail"),
        "extractor":   info.get("extractor_key") or info.get("extractor"),
        "webpage_url": info.get("webpage_url"),
        "duration":    info.get("duration"),
        "formats":     formats,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
