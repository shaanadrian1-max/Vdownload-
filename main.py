"""
Universal Video Extractor API
- Auto-updates yt-dlp on every startup
- Handles YouTube, TikTok, Facebook, Instagram, Twitter and 1000+ sites
- No pydantic, no file storage, pure extractor
"""

import os
import re
import sys
import logging
import subprocess
from typing import Optional, List, Dict, Any

# ── Auto-update yt-dlp on every startup ───────────────────────────────────────
# This ensures we always have the latest version with newest bot-bypass patches
try:
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp", "--quiet"],
        timeout=60,
        check=False
    )
except Exception:
    pass  # If update fails, continue with installed version

import yt_dlp
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger(__name__)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Universal Video Extractor",
    version="2.0.0",
)

# ── CORS — allow all origins ───────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API Secret (optional) ─────────────────────────────────────────────────────
API_SECRET = os.getenv("API_SECRET", "")

def check_secret(x_api_secret: Optional[str] = None) -> None:
    if API_SECRET and x_api_secret != API_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden: invalid API secret.")


# ─────────────────────────────────────────────────────────────────────────────
# yt-dlp options builder
# ─────────────────────────────────────────────────────────────────────────────
def build_ydl_opts(url: str) -> dict:
    url_lower = url.lower()

    is_tiktok    = "tiktok.com"    in url_lower
    is_youtube   = "youtube.com"   in url_lower or "youtu.be" in url_lower
    is_instagram = "instagram.com" in url_lower
    is_facebook  = "facebook.com"  in url_lower or "fb.watch" in url_lower
    is_twitter   = "twitter.com"   in url_lower or "x.com"    in url_lower

    opts: Dict[str, Any] = {
        "quiet":          True,
        "no_warnings":    True,
        "extract_flat":   False,
        "skip_download":  True,      # NEVER download files
        "noplaylist":     True,
        "geo_bypass":     True,
        "socket_timeout": 30,
        "retries":        3,
        "fragment_retries": 3,

        # Rotate user agents to avoid bot detection
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    }

    # ── TikTok: no-watermark ──────────────────────────────────────────────────
    if is_tiktok:
        opts["format"] = (
            "bestvideo[format_id!*=watermark]+bestaudio[format_id!*=watermark]"
            "/best[format_id!*=watermark]"
            "/bestvideo+bestaudio/best"
        )

    # ── YouTube: use android client to bypass bot check ──────────────────────
    elif is_youtube:
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["android", "web"],
            }
        }
        # Use cookies file if provided
        cookies_file = os.getenv("YTDLP_COOKIES_FILE", "")
        if cookies_file and os.path.isfile(cookies_file):
            opts["cookiefile"] = cookies_file

    # ── Instagram: use cookies if available ──────────────────────────────────
    elif is_instagram:
        cookies_file = os.getenv("INSTAGRAM_COOKIES_FILE", "")
        if cookies_file and os.path.isfile(cookies_file):
            opts["cookiefile"] = cookies_file

    # ── Proxy (optional — set YTDLP_PROXY env var) ───────────────────────────
    proxy = os.getenv("YTDLP_PROXY", "")
    if proxy:
        opts["proxy"] = proxy

    return opts


def do_extract(url: str) -> dict:
    opts = build_ydl_opts(url)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return ydl.sanitize_info(info)


def parse_formats(raw: list) -> List[dict]:
    out = []

    for f in raw:
        dl_url = f.get("url") or f.get("manifest_url") or ""
        if not dl_url:
            continue

        # Skip HLS manifests (require ffmpeg on client)
        if dl_url.endswith(".m3u8"):
            continue
        if f.get("protocol") in ("m3u8", "m3u8_native"):
            continue

        w = f.get("width")
        h = f.get("height")
        resolution = f.get("resolution") or (f"{w}x{h}" if w and h else None)

        out.append({
            "format_id":   str(f.get("format_id", "")),
            "ext":         str(f.get("ext", "")),
            "resolution":  resolution,
            "format_note": f.get("format_note"),
            "filesize":    f.get("filesize") or f.get("filesize_approx"),
            "url":         dl_url,
            "vcodec":      f.get("vcodec"),
            "acodec":      f.get("acodec"),
            "tbr":         f.get("tbr"),
            "abr":         f.get("abr"),
            "fps":         f.get("fps"),
        })

    # Deduplicate
    seen: Dict[str, dict] = {}
    for item in out:
        key = f"{item['resolution']}|{item['ext']}"
        existing = seen.get(key)
        if not existing or (item["filesize"] or 0) > (existing["filesize"] or 0):
            seen[key] = item

    # Sort: video (best res first), then audio-only
    def sort_key(fi: dict):
        audio_only = not fi["vcodec"] or fi["vcodec"] == "none"
        h = 0
        if fi["resolution"]:
            m = re.search(r"(\d+)x(\d+)", fi["resolution"])
            if m:
                h = int(m.group(2))
        return (1 if audio_only else 0, -h)

    return sorted(seen.values(), key=sort_key)


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "status":  "ok",
        "message": "Video Extractor API is running.",
        "version": "2.0.0",
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/extract")
async def extract(
    body: dict,
    x_api_secret: Optional[str] = Header(default=None),
):
    check_secret(x_api_secret)

    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required.")

    # Basic URL validation
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Please provide a valid URL starting with http:// or https://")

    logger.info(f"Extracting: {url}")

    try:
        info = do_extract(url)

    except yt_dlp.utils.DownloadError as e:
        err = str(e).lower()
        logger.warning(f"yt-dlp error for {url}: {e}")

        if "unsupported url" in err:
            raise HTTPException(status_code=422, detail="This URL is not supported. Please try a direct video link from YouTube, TikTok, Instagram, etc.")
        if "private" in err or "login" in err or "sign in" in err:
            raise HTTPException(status_code=422, detail="This video is private or requires login. Only public videos are supported.")
        if "unavailable" in err or "removed" in err:
            raise HTTPException(status_code=422, detail="This video is unavailable or has been removed.")
        if "rate" in err or "429" in err or "too many" in err:
            raise HTTPException(status_code=429, detail="Rate limited by the platform. Please try again in a few minutes.")
        if "copyright" in err or "blocked" in err:
            raise HTTPException(status_code=422, detail="This video is blocked or restricted in this region.")

        raise HTTPException(status_code=422, detail=f"Could not extract video: {str(e)[:300]}")

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)[:200]}")

    raw_formats = info.get("formats") or []

    # Fallback: single-URL response
    if not raw_formats and info.get("url"):
        raw_formats = [info]

    formats = parse_formats(raw_formats)

    if not formats:
        raise HTTPException(
            status_code=422,
            detail="No downloadable formats found. The video may be restricted or require authentication."
        )

    return {
        "title":       info.get("title") or "Untitled",
        "thumbnail":   info.get("thumbnail"),
        "extractor":   info.get("extractor_key") or info.get("extractor"),
        "webpage_url": info.get("webpage_url") or url,
        "duration":    info.get("duration"),
        "uploader":    info.get("uploader"),
        "formats":     formats,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
