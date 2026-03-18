import os
import re
import logging
import tempfile
import threading
from typing import Optional, Dict, Any
from pathlib import Path

import yt_dlp
from fastapi import FastAPI, HTTPException, Header, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Video Extractor", version="5.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

API_SECRET = os.getenv("API_SECRET", "")


def check_secret(s: Optional[str]):
    if API_SECRET and s != API_SECRET:
        raise HTTPException(403, "Forbidden.")


# ─────────────────────────────────────────────────────────────────────────────
# Common yt-dlp headers
# ─────────────────────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# ─────────────────────────────────────────────────────────────────────────────
# /extract — returns all available qualities as downloadable items
# Each item points to /dl?id=xxx for actual download
# ─────────────────────────────────────────────────────────────────────────────
def get_info_opts(url: str) -> dict:
    url_l = url.lower()
    is_yt = "youtube.com" in url_l or "youtu.be" in url_l

    opts: Dict[str, Any] = {
        "quiet":         True,
        "no_warnings":   True,
        "skip_download": True,
        "noplaylist":    True,
        "geo_bypass":    True,
        "socket_timeout":30,
        "http_headers":  HEADERS,
    }

    if is_yt:
        opts["extractor_args"] = {
            "youtube": {"player_client": ["android", "web"]}
        }
        cf = os.getenv("YTDLP_COOKIES_FILE", "")
        if cf and os.path.isfile(cf):
            opts["cookiefile"] = cf

    proxy = os.getenv("YTDLP_PROXY", "")
    if proxy:
        opts["proxy"] = proxy

    return opts


def extract_info(url: str) -> dict:
    with yt_dlp.YoutubeDL(get_info_opts(url)) as ydl:
        info = ydl.extract_info(url, download=False)
        return ydl.sanitize_info(info)


def height_of(res: Optional[str]) -> int:
    if not res:
        return 0
    m = re.search(r"(\d+)x(\d+)", res)
    return int(m.group(2)) if m else 0


def build_format_list(raw: list, info: dict, base_url: str) -> dict:
    """
    Build video and audio lists.
    Each item gets a /dl proxy URL instead of the raw CDN URL.
    This solves CORS + IP-bound URL problems.
    """
    video_out  = []
    audio_out  = []
    seen_v: set = set()
    seen_a: set = set()

    orig_url = info.get("webpage_url") or info.get("original_url") or ""

    for f in raw:
        dl_url = f.get("url") or ""
        if not dl_url:
            continue

        proto = f.get("protocol") or ""
        if proto in ("m3u8", "m3u8_native", "dash", "rtsp"):
            continue
        if dl_url.endswith(".m3u8"):
            continue

        vc = f.get("vcodec") or "none"
        ac = f.get("acodec") or "none"
        has_v = vc != "none"
        has_a = ac != "none"

        w = f.get("width")
        h = f.get("height")
        res = f.get("resolution") or (f"{w}x{h}" if w and h else None)

        # Build proxy download URL
        fmt_id  = str(f.get("format_id", "best"))
        ext     = str(f.get("ext", "mp4"))
        dl_proxy = f"{base_url}/dl?url={_enc(orig_url)}&fmt={_enc(fmt_id)}&ext={_enc(ext)}"

        item = {
            "format_id":   fmt_id,
            "ext":         ext,
            "resolution":  res,
            "format_note": f.get("format_note"),
            "filesize":    f.get("filesize") or f.get("filesize_approx"),
            "url":         dl_proxy,   # ← proxy URL, not raw CDN
            "vcodec":      vc,
            "acodec":      ac,
            "abr":         f.get("abr"),
            "has_audio":   has_a,
        }

        if has_v:
            key = res or fmt_id
            if key not in seen_v:
                seen_v.add(key)
                video_out.append(item)
        elif has_a:
            abr = f.get("abr") or 0
            key = f"{abr}|{ext}"
            if key not in seen_a:
                seen_a.add(key)
                audio_out.append(item)

    # If no video found, make one "Best" entry
    if not video_out:
        fmt_id = "best"
        ext    = info.get("ext", "mp4")
        w = info.get("width"); h = info.get("height")
        res = f"{w}x{h}" if w and h else "Best"
        dl_proxy = f"{base_url}/dl?url={_enc(orig_url)}&fmt={_enc(fmt_id)}&ext={_enc(ext)}"
        video_out.append({
            "format_id": fmt_id, "ext": ext, "resolution": res,
            "format_note": "Best Quality", "filesize": None,
            "url": dl_proxy, "vcodec": "avc1", "acodec": "mp4a",
            "abr": None, "has_audio": True,
        })

    # Sort: video high→low res, audio high→low bitrate
    video_out.sort(key=lambda x: -height_of(x.get("resolution")))
    audio_out.sort(key=lambda x: -(x.get("abr") or 0))

    return {"video": video_out, "audio": audio_out}


def _enc(s: str) -> str:
    from urllib.parse import quote
    return quote(str(s), safe="")


# ─────────────────────────────────────────────────────────────────────────────
# /dl — proxy download endpoint
# yt-dlp downloads the actual file on Railway, streams it back to browser
# This bypasses CORS and IP-bound URL issues completely
# ─────────────────────────────────────────────────────────────────────────────
def dl_opts(fmt_id: str, out_path: str, orig_url: str) -> dict:
    url_l = orig_url.lower()
    is_yt = "youtube.com" in url_l or "youtu.be" in url_l

    opts: Dict[str, Any] = {
        "quiet":         True,
        "no_warnings":   True,
        "noplaylist":    True,
        "geo_bypass":    True,
        "socket_timeout":60,
        "format":        fmt_id,
        "outtmpl":       out_path,
        "http_headers":  HEADERS,
        # Merge video+audio with ffmpeg if needed (ffmpeg installed via apt.txt)
        "merge_output_format": "mp4",
        "postprocessors": [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }],
    }

    if is_yt:
        opts["extractor_args"] = {
            "youtube": {"player_client": ["android", "web"]}
        }
        cf = os.getenv("YTDLP_COOKIES_FILE", "")
        if cf and os.path.isfile(cf):
            opts["cookiefile"] = cf

    proxy = os.getenv("YTDLP_PROXY", "")
    if proxy:
        opts["proxy"] = proxy

    return opts


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"status": "ok", "message": "Video Extractor API v5.0 running."}

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/extract")
async def extract_endpoint(
    request: dict,
    x_api_secret: Optional[str] = Header(default=None),
):
    check_secret(x_api_secret)

    url = (request.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "URL is required.")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Invalid URL.")

    logger.info(f"Extracting: {url}")

    try:
        info = extract_info(url)
    except yt_dlp.utils.DownloadError as e:
        err = str(e).lower()
        if "unsupported url"   in err: raise HTTPException(422, "This URL is not supported.")
        if "private"           in err or "login" in err: raise HTTPException(422, "Private video or login required.")
        if "unavailable"       in err: raise HTTPException(422, "Video unavailable or removed.")
        if "429"               in err or "rate" in err: raise HTTPException(429, "Rate limited. Try again later.")
        raise HTTPException(422, f"Extraction failed: {str(e)[:200]}")
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        raise HTTPException(500, f"Server error: {str(e)[:150]}")

    raw = info.get("formats") or []
    if not raw and info.get("url"):
        raw = [info]

    # Detect base URL from Railway env
    base = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
    if base:
        base = f"https://{base}"
    else:
        base = os.getenv("API_BASE_URL", "https://web-production-6fd4b.up.railway.app")

    parsed = build_format_list(raw, info, base)
    vf = parsed["video"]
    af = parsed["audio"]

    if not vf and not af:
        raise HTTPException(422, "No downloadable formats found.")

    return {
        "title":       info.get("title") or "Untitled",
        "thumbnail":   info.get("thumbnail"),
        "extractor":   info.get("extractor_key") or info.get("extractor"),
        "webpage_url": info.get("webpage_url") or url,
        "duration":    info.get("duration"),
        "uploader":    info.get("uploader"),
        "formats":     vf + af,
    }


@app.get("/dl")
async def download_proxy(
    url:  str,
    fmt:  str = "best",
    ext:  str = "mp4",
    background_tasks: BackgroundTasks = None,
):
    """
    Downloads the video via yt-dlp on Railway server,
    then streams the file back to the browser as an attachment.
    Supports merging video+audio via ffmpeg.
    """
    if not url or not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Invalid URL.")

    logger.info(f"Proxy DL: fmt={fmt} url={url[:80]}")

    # Use temp dir (Railway ephemeral storage is fine — we stream then delete)
    tmp_dir  = tempfile.mkdtemp()
    out_tmpl = os.path.join(tmp_dir, "video.%(ext)s")

    try:
        opts = dl_opts(fmt, out_tmpl, url)
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:
        logger.error(f"Proxy DL error: {e}")
        raise HTTPException(500, f"Download failed: {str(e)[:150]}")

    # Find downloaded file
    files = list(Path(tmp_dir).glob("*"))
    if not files:
        raise HTTPException(500, "Downloaded file not found.")

    out_file = str(files[0])
    actual_ext = Path(out_file).suffix.lstrip(".") or ext
    filename   = f"video.{actual_ext}"

    def cleanup():
        try:
            os.unlink(out_file)
            os.rmdir(tmp_dir)
        except Exception:
            pass

    def stream_file():
        try:
            with open(out_file, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk
        finally:
            cleanup()

    mime = "video/mp4" if actual_ext == "mp4" else "application/octet-stream"

    return StreamingResponse(
        stream_file(),
        media_type=mime,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control":       "no-cache",
        }
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
