"""
Universal Video Extractor API v3.0
- Returns COMBINED video+audio streams only (no silent video)
- Separate audio-only streams also provided
- Auto-updates yt-dlp on startup
"""

import os
import re
import sys
import logging
import subprocess
from typing import Optional, List, Dict, Any

# ── Auto-update yt-dlp ────────────────────────────────────────────────────────
try:
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp", "--quiet"],
        timeout=60, check=False
    )
except Exception:
    pass

import yt_dlp
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Universal Video Extractor", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

API_SECRET = os.getenv("API_SECRET", "")

def check_secret(x_api_secret: Optional[str] = None):
    if API_SECRET and x_api_secret != API_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden.")


# ─────────────────────────────────────────────────────────────────────────────
# yt-dlp options
# ─────────────────────────────────────────────────────────────────────────────
def build_ydl_opts(url: str) -> dict:
    url_lower = url.lower()
    is_tiktok  = "tiktok.com"  in url_lower
    is_youtube = "youtube.com" in url_lower or "youtu.be" in url_lower

    opts: Dict[str, Any] = {
        "quiet":          True,
        "no_warnings":    True,
        "extract_flat":   False,
        "skip_download":  True,
        "noplaylist":     True,
        "geo_bypass":     True,
        "socket_timeout": 30,
        "retries":        3,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    if is_tiktok:
        # TikTok: no watermark, combined stream
        opts["format"] = (
            "best[format_id!*=watermark]"
            "/bestvideo[format_id!*=watermark]+bestaudio[format_id!*=watermark]"
            "/best"
        )
    elif is_youtube:
        # YouTube: android client bypasses bot check
        opts["extractor_args"] = {
            "youtube": {"player_client": ["android", "web"]}
        }
        cookies_file = os.getenv("YTDLP_COOKIES_FILE", "")
        if cookies_file and os.path.isfile(cookies_file):
            opts["cookiefile"] = cookies_file
    else:
        # All other sites: prefer combined stream with audio
        opts["format"] = "best/bestvideo+bestaudio"

    proxy = os.getenv("YTDLP_PROXY", "")
    if proxy:
        opts["proxy"] = proxy

    return opts


def do_extract(url: str) -> dict:
    opts = build_ydl_opts(url)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return ydl.sanitize_info(info)


# ─────────────────────────────────────────────────────────────────────────────
# Format parsing — KEY FIX HERE
# ─────────────────────────────────────────────────────────────────────────────
def parse_formats(raw: list) -> dict:
    """
    Returns two lists:
    - video_formats: streams that have BOTH video AND audio (sound included!)
    - audio_formats: audio-only streams (for music extraction)
    """
    video_formats = []  # combined video+audio
    audio_formats = []  # audio only

    seen_video = set()
    seen_audio = set()

    for f in raw:
        dl_url = f.get("url") or ""
        if not dl_url:
            continue

        # Skip HLS/DASH manifests (need ffmpeg on client to play)
        protocol = f.get("protocol") or ""
        if protocol in ("m3u8", "m3u8_native", "dash") or dl_url.endswith(".m3u8"):
            continue

        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        has_video = vcodec != "none"
        has_audio = acodec != "none"

        w = f.get("width")
        h = f.get("height")
        resolution = f.get("resolution") or (f"{w}x{h}" if w and h else None)

        fmt = {
            "format_id":   str(f.get("format_id", "")),
            "ext":         str(f.get("ext", "mp4")),
            "resolution":  resolution,
            "format_note": f.get("format_note"),
            "filesize":    f.get("filesize") or f.get("filesize_approx"),
            "url":         dl_url,
            "vcodec":      vcodec,
            "acodec":      acodec,
            "tbr":         f.get("tbr"),
            "abr":         f.get("abr"),
            "fps":         f.get("fps"),
            "has_audio":   has_audio,  # important flag for frontend
        }

        if has_video and has_audio:
            # ✅ COMBINED stream — video WITH sound
            key = f"{resolution}|{fmt['ext']}"
            if key not in seen_video:
                seen_video.add(key)
                video_formats.append(fmt)

        elif has_video and not has_audio:
            # ❌ Video-only (silent) — SKIP, don't show to user
            pass

        elif not has_video and has_audio:
            # 🎵 Audio-only stream
            key = f"{fmt['abr']}|{fmt['ext']}"
            if key not in seen_audio:
                seen_audio.add(key)
                audio_formats.append(fmt)

    # Sort video by resolution (highest first)
    def video_sort(fi):
        h = 0
        if fi["resolution"]:
            m = re.search(r"(\d+)x(\d+)", fi["resolution"])
            if m:
                h = int(m.group(2))
        return -h

    # Sort audio by bitrate (highest first)
    def audio_sort(fi):
        return -(fi.get("abr") or 0)

    video_formats.sort(key=video_sort)
    audio_formats.sort(key=audio_sort)

    return {
        "video": video_formats,
        "audio": audio_formats,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"status": "ok", "message": "Video Extractor API is running.", "version": "3.0.0"}

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
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Please provide a valid URL.")

    logger.info(f"Extracting: {url}")

    try:
        info = do_extract(url)
    except yt_dlp.utils.DownloadError as e:
        err = str(e).lower()
        logger.warning(f"yt-dlp error: {e}")
        if "unsupported url" in err:
            raise HTTPException(status_code=422, detail="This URL is not supported.")
        if "private" in err or "login" in err or "sign in" in err:
            raise HTTPException(status_code=422, detail="This video is private or requires login.")
        if "unavailable" in err or "removed" in err:
            raise HTTPException(status_code=422, detail="This video is unavailable or removed.")
        if "429" in err or "rate" in err:
            raise HTTPException(status_code=429, detail="Rate limited. Please try again in a few minutes.")
        raise HTTPException(status_code=422, detail=f"Extraction failed: {str(e)[:200]}")
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)[:150]}")

    raw_formats = info.get("formats") or []
    if not raw_formats and info.get("url"):
        raw_formats = [info]

    parsed = parse_formats(raw_formats)
    video_fmts = parsed["video"]
    audio_fmts = parsed["audio"]

    # If NO combined streams found (e.g. YouTube returns split streams)
    # merge them into a single "best" entry using the info dict directly
    if not video_fmts:
        # yt-dlp's top-level url is usually the best combined format
        top_url = info.get("url") or ""
        if top_url and not top_url.endswith(".m3u8"):
            w = info.get("width")
            h = info.get("height")
            video_fmts = [{
                "format_id":   "best",
                "ext":         info.get("ext") or "mp4",
                "resolution":  f"{w}x{h}" if w and h else info.get("resolution") or "Best",
                "format_note": "Best Quality",
                "filesize":    info.get("filesize"),
                "url":         top_url,
                "vcodec":      info.get("vcodec"),
                "acodec":      info.get("acodec"),
                "tbr":         info.get("tbr"),
                "abr":         info.get("abr"),
                "fps":         info.get("fps"),
                "has_audio":   True,
            }]

    if not video_fmts and not audio_fmts:
        raise HTTPException(status_code=422, detail="No downloadable formats found.")

    # Return in old flat format so frontend still works
    # but mark each with has_audio flag
    all_formats = video_fmts + audio_fmts

    return {
        "title":       info.get("title") or "Untitled",
        "thumbnail":   info.get("thumbnail"),
        "extractor":   info.get("extractor_key") or info.get("extractor"),
        "webpage_url": info.get("webpage_url") or url,
        "duration":    info.get("duration"),
        "uploader":    info.get("uploader"),
        "formats":     all_formats,
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
