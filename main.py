"""
Railway FastAPI Backend — Universal Video Extractor
Uses yt-dlp (Python library) to extract video/audio download links.
NO files are stored on disk; this is a pure extractor/metadata proxy.
"""

import os
import re
import logging
from typing import Optional, List, Dict, Any

import yt_dlp
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, AnyHttpUrl

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
# Change "*" to your WordPress domain in production, e.g.
# allowed_origins = ["https://yoursite.com"]
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,  # e.g. ["https://yoursite.com"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Optional API secret guard ─────────────────────────────────────────────────
API_SECRET = os.getenv("API_SECRET", "")   # Set in Railway env vars


def check_secret(x_api_secret: Optional[str] = None) -> None:
    if API_SECRET and x_api_secret != API_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden: invalid API secret.")


# ─────────────────────────────────────────────────────────────────────────────
# Request / Response schemas
# ─────────────────────────────────────────────────────────────────────────────
class ExtractRequest(BaseModel):
    url: str


class FormatInfo(BaseModel):
    format_id:   str
    ext:         str
    resolution:  Optional[str]  = None
    format_note: Optional[str]  = None
    filesize:    Optional[int]  = None
    url:         Optional[str]  = None   # direct download URL
    vcodec:      Optional[str]  = None
    acodec:      Optional[str]  = None
    tbr:         Optional[float] = None  # total bitrate kbps
    abr:         Optional[float] = None  # audio bitrate kbps
    fps:         Optional[float] = None


class ExtractResponse(BaseModel):
    title:       str
    thumbnail:   Optional[str]  = None
    extractor:   Optional[str]  = None
    webpage_url: Optional[str]  = None
    duration:    Optional[float] = None
    formats:     List[FormatInfo] = []


# ─────────────────────────────────────────────────────────────────────────────
# yt-dlp helper
# ─────────────────────────────────────────────────────────────────────────────
def build_ydl_opts(url: str) -> dict:
    """
    Build yt-dlp options dict.

    Key decisions:
    - 'extract_flat': False  → get real URLs for every format
    - 'quiet': True          → suppress console spam
    - 'no_warnings': True
    - For TikTok: we explicitly prefer the non-watermarked format by
      setting the format selector to favour format_id containing 'nowm'
      or 'play_addr_h264' which are the clean copies yt-dlp can reach.
    """
    is_tiktok = bool(re.search(r'tiktok\.com', url, re.I))

    opts: Dict[str, Any] = {
        "quiet":         True,
        "no_warnings":   True,
        "extract_flat":  False,
        "skip_download": True,       # ← CRITICAL: never write files to disk
        "noplaylist":    True,       # single video only
        "geo_bypass":    True,
        # Give each network call a timeout so we don't hang indefinitely
        "socket_timeout": 30,
    }

    # ── TikTok: No-Watermark strategy ────────────────────────────────────────
    # yt-dlp names the watermark-free stream differently on TikTok.
    # We prefer format IDs that contain 'play_addr' (clean) over
    # 'watermark' variants. Setting format to a custom selector handles this.
    if is_tiktok:
        # This selector asks for the best quality that does NOT have
        # 'watermark' in its format_id (yt-dlp's TikTok extractor labels them).
        opts["format"] = (
            "bestvideo[format_id!*=watermark]+bestaudio[format_id!*=watermark]"
            "/best[format_id!*=watermark]"
            "/bestvideo+bestaudio/best"
        )

    # ── Proxy / cookies (optional — see env vars) ─────────────────────────────
    proxy = os.getenv("YTDLP_PROXY", "")
    if proxy:
        opts["proxy"] = proxy

    cookies_file = os.getenv("YTDLP_COOKIES_FILE", "")   # path to cookies.txt
    if cookies_file and os.path.isfile(cookies_file):
        opts["cookiefile"] = cookies_file

    # ── HTTP headers to reduce bot detection ─────────────────────────────────
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
        # Sanitise the dict (removes non-serialisable objects)
        return ydl.sanitize_info(info)


def parse_formats(raw_formats: list) -> List[FormatInfo]:
    results: List[FormatInfo] = []

    for f in raw_formats:
        # Must have a direct URL to be useful
        fmt_url = f.get("url") or f.get("manifest_url") or ""
        if not fmt_url:
            continue

        # Skip m3u8 manifest-only entries (they require ffmpeg on the client)
        if fmt_url.endswith(".m3u8") or "manifest" in fmt_url.lower():
            continue

        resolution = f.get("resolution") or (
            f"{f['width']}x{f['height']}" if f.get("width") and f.get("height") else None
        )

        results.append(FormatInfo(
            format_id=   f.get("format_id", ""),
            ext=         f.get("ext", ""),
            resolution=  resolution,
            format_note= f.get("format_note"),
            filesize=    f.get("filesize") or f.get("filesize_approx"),
            url=         fmt_url,
            vcodec=      f.get("vcodec"),
            acodec=      f.get("acodec"),
            tbr=         f.get("tbr"),
            abr=         f.get("abr"),
            fps=         f.get("fps"),
        ))

    # Deduplicate by (resolution, ext) keeping largest filesize
    seen: Dict[str, FormatInfo] = {}
    for item in results:
        key = f"{item.resolution}|{item.ext}"
        existing = seen.get(key)
        if not existing:
            seen[key] = item
        else:
            sz_new = item.filesize or 0
            sz_old = existing.filesize or 0
            if sz_new > sz_old:
                seen[key] = item

    # Sort: video (highest res) first, then audio-only
    def sort_key(fi: FormatInfo):
        is_audio_only = (not fi.vcodec or fi.vcodec == "none")
        height = 0
        if fi.resolution:
            m = re.search(r"(\d+)x(\d+)", fi.resolution)
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


@app.post("/extract", response_model=ExtractResponse)
async def extract_endpoint(
    body: ExtractRequest,
    x_api_secret: Optional[str] = Header(default=None),
):
    """
    Extract available formats from a video URL.

    Returns structured JSON with title, thumbnail, and a list of
    video and audio-only formats — each with a direct download URL.

    ⚠️  Direct URLs are usually time-limited (minutes to hours).
        The client must start the download promptly.
    """
    check_secret(x_api_secret)

    url = body.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required.")

    logger.info(f"Extracting: {url}")

    try:
        info = extract_info(url)
    except yt_dlp.utils.DownloadError as e:
        err_str = str(e)
        logger.warning(f"yt-dlp DownloadError: {err_str}")

        # Surface friendly messages for common errors
        if "Unsupported URL" in err_str or "No video formats" in err_str:
            raise HTTPException(
                status_code=422,
                detail="This URL is not supported. Please try a direct video link from YouTube, TikTok, Instagram, etc.",
            )
        if "Private video" in err_str or "login required" in err_str.lower():
            raise HTTPException(
                status_code=422,
                detail="This video is private or requires login. Only public videos are supported.",
            )
        if "Video unavailable" in err_str:
            raise HTTPException(status_code=422, detail="This video is unavailable or has been removed.")

        raise HTTPException(status_code=422, detail=f"Extraction failed: {err_str[:200]}")

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred. Please try again.")

    # ── Build response ────────────────────────────────────────────────────────
    raw_formats: list = info.get("formats") or []

    # If yt-dlp returned a single-format dict instead of a formats list
    if not raw_formats and info.get("url"):
        raw_formats = [info]

    formats = parse_formats(raw_formats)

    if not formats:
        raise HTTPException(
            status_code=422,
            detail="No downloadable formats were found for this URL.",
        )

    return ExtractResponse(
        title=       info.get("title", "Untitled"),
        thumbnail=   info.get("thumbnail"),
        extractor=   info.get("extractor_key") or info.get("extractor"),
        webpage_url= info.get("webpage_url"),
        duration=    info.get("duration"),
        formats=     formats,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point for Railway
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
