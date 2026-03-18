import os
import re
import logging
import tempfile
from typing import Optional, Dict, Any
from pathlib import Path
from urllib.parse import quote, unquote

import yt_dlp
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Video Extractor", version="6.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

API_SECRET = os.getenv("API_SECRET", "")
BASE_URL   = os.getenv("API_BASE_URL", "https://web-production-6fd4b.up.railway.app")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

def check_secret(s: Optional[str]):
    if API_SECRET and s != API_SECRET:
        raise HTTPException(403, "Forbidden.")

def is_yt(url): return "youtube.com" in url or "youtu.be" in url
def is_fb(url): return "facebook.com" in url or "fb.watch" in url
def is_tt(url): return "tiktok.com" in url

def enc(s): return quote(str(s), safe="")
def height_of(res):
    if not res: return 0
    m = re.search(r"(\d+)x(\d+)", str(res))
    return int(m.group(2)) if m else 0


# ─────────────────────────────────────────────────────────────────────────────
# EXTRACT — get info + format list
# ─────────────────────────────────────────────────────────────────────────────
def extract_info(url: str) -> dict:
    opts: Dict[str, Any] = {
        "quiet": True, "no_warnings": True,
        "skip_download": True, "noplaylist": True,
        "geo_bypass": True, "socket_timeout": 30,
        "http_headers": HEADERS,
    }
    if is_yt(url):
        opts["extractor_args"] = {"youtube": {"player_client": ["android", "web"]}}
        cf = os.getenv("YTDLP_COOKIES_FILE", "")
        if cf and os.path.isfile(cf): opts["cookiefile"] = cf
    proxy = os.getenv("YTDLP_PROXY", "")
    if proxy: opts["proxy"] = proxy

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return ydl.sanitize_info(info)


def build_dl_url(orig_url: str, fmt_selector: str, ext: str, label: str) -> str:
    """Build a /dl proxy URL."""
    base = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
    base = f"https://{base}" if base else BASE_URL
    return f"{base}/dl?url={enc(orig_url)}&fmt={enc(fmt_selector)}&ext={enc(ext)}&label={enc(label)}"


def parse_formats(raw: list, info: dict) -> dict:
    """
    Returns:
      video: list of combined video+audio items (sorted best→worst)
      audio: list of audio-only items
    """
    orig_url = info.get("webpage_url") or info.get("original_url") or ""
    url_lower = orig_url.lower()

    video_items = []
    audio_items = []
    seen_v: set = set()
    seen_a: set = set()

    for f in raw:
        if not f.get("url"): continue
        proto = f.get("protocol") or ""
        if proto in ("m3u8", "m3u8_native", "dash", "rtsp"): continue
        if (f.get("url") or "").endswith(".m3u8"): continue

        vc = f.get("vcodec") or "none"
        ac = f.get("acodec") or "none"
        has_v = vc != "none"
        has_a = ac != "none"

        w = f.get("width"); h = f.get("height")
        res  = f.get("resolution") or (f"{w}x{h}" if w and h else None)
        ext  = str(f.get("ext") or "mp4")
        fid  = str(f.get("format_id") or "best")
        note = f.get("format_note") or ""
        abr  = f.get("abr") or 0

        if has_v and has_a:
            # Combined stream — use format_id for /dl
            key = res or fid
            if key not in seen_v:
                seen_v.add(key)
                label = res or note or fid
                video_items.append({
                    "format_id":   fid,
                    "ext":         ext,
                    "resolution":  res,
                    "format_note": note,
                    "filesize":    f.get("filesize") or f.get("filesize_approx"),
                    "url":         build_dl_url(orig_url, fid, ext, label),
                    "vcodec":      vc,
                    "acodec":      ac,
                    "abr":         abr,
                    "type":        "video",
                })

        elif has_v and not has_a:
            # Video-only — skip for display, but use for YouTube HD merging
            # We'll add merged entries below
            pass

        elif not has_v and has_a:
            # Audio only
            key = f"{abr:.0f}|{ext}"
            if key not in seen_a:
                seen_a.add(key)
                abr_label = f"{abr:.0f}kbps" if abr else ext
                audio_items.append({
                    "format_id":   fid,
                    "ext":         ext,
                    "resolution":  None,
                    "format_note": note or abr_label,
                    "filesize":    f.get("filesize") or f.get("filesize_approx"),
                    "url":         build_dl_url(orig_url, fid, ext, abr_label),
                    "vcodec":      "none",
                    "acodec":      ac,
                    "abr":         abr,
                    "type":        "audio",
                })

    # ── YouTube: add merged HD entries using format selectors ──────────────────
    if is_yt(url_lower):
        yt_qualities = [
            ("bestvideo[height>=2160]+bestaudio/best[height>=2160]", "mp4", "3840x2160", "4K"),
            ("bestvideo[height>=1080]+bestaudio/best[height>=1080]", "mp4", "1920x1080", "1080p"),
            ("bestvideo[height>=720]+bestaudio/best[height>=720]",   "mp4", "1280x720",  "720p"),
            ("bestvideo[height>=480]+bestaudio/best[height>=480]",   "mp4", "854x480",   "480p"),
            ("bestvideo[height>=360]+bestaudio/best[height>=360]",   "mp4", "640x360",   "360p"),
        ]
        # Only add qualities that weren't already found as combined
        existing_heights = {height_of(v.get("resolution")) for v in video_items}

        for fmt_sel, ext, res, label in yt_qualities:
            h = height_of(res)
            if h not in existing_heights:
                video_items.append({
                    "format_id":   fmt_sel,
                    "ext":         ext,
                    "resolution":  res,
                    "format_note": label,
                    "filesize":    None,
                    "url":         build_dl_url(orig_url, fmt_sel, ext, label),
                    "vcodec":      "avc1",
                    "acodec":      "mp4a",
                    "abr":         None,
                    "type":        "video",
                })

        # YouTube audio
        if not audio_items:
            audio_items.append({
                "format_id":   "bestaudio/best",
                "ext":         "m4a",
                "resolution":  None,
                "format_note": "Best Audio",
                "filesize":    None,
                "url":         build_dl_url(orig_url, "bestaudio/best", "m4a", "Best Audio"),
                "vcodec":      "none",
                "acodec":      "aac",
                "abr":         None,
                "type":        "audio",
            })

    # ── Fallback: no video found at all ───────────────────────────────────────
    if not video_items:
        top = info.get("url", "")
        if top and not top.endswith(".m3u8"):
            w = info.get("width"); h = info.get("height")
            res = f"{w}x{h}" if w and h else "Best"
            video_items.append({
                "format_id":   "best",
                "ext":         info.get("ext") or "mp4",
                "resolution":  res,
                "format_note": "Best Quality",
                "filesize":    info.get("filesize"),
                "url":         build_dl_url(orig_url, "best", info.get("ext") or "mp4", "Best"),
                "vcodec":      "avc1",
                "acodec":      "mp4a",
                "abr":         None,
                "type":        "video",
            })

    # Sort
    video_items.sort(key=lambda x: -height_of(x.get("resolution")))
    audio_items.sort(key=lambda x: -(x.get("abr") or 0))

    return {"video": video_items, "audio": audio_items}


# ─────────────────────────────────────────────────────────────────────────────
# /dl — proxy download endpoint
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/dl")
async def download_proxy(url: str, fmt: str = "best", ext: str = "mp4", label: str = "video"):
    if not url or not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Invalid URL.")

    logger.info(f"DL: fmt={fmt[:60]} url={url[:80]}")

    tmp_dir  = tempfile.mkdtemp()
    out_tmpl = os.path.join(tmp_dir, "%(title).50s.%(ext)s")

    opts: Dict[str, Any] = {
        "quiet":           True,
        "no_warnings":     True,
        "noplaylist":      True,
        "socket_timeout":  60,
        "format":          fmt,
        "outtmpl":         out_tmpl,
        "http_headers":    HEADERS,
        # Merge split streams with ffmpeg
        "merge_output_format": "mp4",
    }

    if is_yt(url):
        opts["extractor_args"] = {"youtube": {"player_client": ["android", "web"]}}
        cf = os.getenv("YTDLP_COOKIES_FILE", "")
        if cf and os.path.isfile(cf): opts["cookiefile"] = cf
    elif is_fb(url):
        # Facebook: don't use specific format_id, use best available
        if not fmt.startswith("bestvideo") and not fmt.startswith("bestaudio") and fmt != "best":
            opts["format"] = "best"
    
    proxy = os.getenv("YTDLP_PROXY", "")
    if proxy: opts["proxy"] = proxy

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:
        logger.error(f"DL error: {e}")
        # Try fallback with "best"
        try:
            opts["format"] = "best"
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except Exception as e2:
            raise HTTPException(500, f"Download failed: {str(e2)[:150]}")

    files = list(Path(tmp_dir).glob("*"))
    if not files:
        raise HTTPException(500, "Downloaded file not found.")

    out_file   = str(max(files, key=lambda p: p.stat().st_size))
    actual_ext = Path(out_file).suffix.lstrip(".") or ext

    # Clean filename
    clean_label = re.sub(r'[^\w\s\-]', '', label).strip()[:50] or "video"
    filename    = f"{clean_label}.{actual_ext}"

    def stream_and_cleanup():
        try:
            with open(out_file, "rb") as f:
                while True:
                    chunk = f.read(1024 * 64)
                    if not chunk: break
                    yield chunk
        finally:
            try:
                import shutil
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass

    mime = "audio/mpeg" if actual_ext in ("mp3","m4a","aac","opus","webm") and "audio" in label.lower() else "video/mp4"

    return StreamingResponse(
        stream_and_cleanup(),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"status": "ok", "message": "Video Extractor API v6.0 running."}

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/extract")
async def extract_endpoint(request: dict, x_api_secret: Optional[str] = Header(default=None)):
    check_secret(x_api_secret)

    url = (request.get("url") or "").strip()
    if not url: raise HTTPException(400, "URL is required.")
    if not url.startswith(("http://","https://")): raise HTTPException(400, "Invalid URL.")

    logger.info(f"Extracting: {url}")

    try:
        info = extract_info(url)
    except yt_dlp.utils.DownloadError as e:
        err = str(e).lower()
        if "unsupported url"  in err: raise HTTPException(422, "This URL is not supported.")
        if "private"          in err or "login" in err: raise HTTPException(422, "Private video or login required.")
        if "unavailable"      in err: raise HTTPException(422, "Video unavailable or removed.")
        if "429"              in err: raise HTTPException(429, "Rate limited. Try again later.")
        raise HTTPException(422, f"Extraction failed: {str(e)[:200]}")
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        raise HTTPException(500, f"Server error: {str(e)[:150]}")

    raw = info.get("formats") or []
    if not raw and info.get("url"): raw = [info]

    parsed = parse_formats(raw, info)
    vf = parsed["video"]
    af = parsed["audio"]

    if not vf and not af: raise HTTPException(422, "No downloadable formats found.")

    return {
        "title":       info.get("title") or "Untitled",
        "thumbnail":   info.get("thumbnail"),
        "extractor":   info.get("extractor_key") or info.get("extractor"),
        "webpage_url": info.get("webpage_url") or url,
        "duration":    info.get("duration"),
        "formats":     vf + af,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
