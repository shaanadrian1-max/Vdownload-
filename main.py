import os
import re
import logging
from typing import Optional, List, Dict, Any

import yt_dlp
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Video Extractor", version="4.0.0")

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


def build_opts(url: str) -> dict:
    url_l = url.lower()
    is_yt = "youtube.com" in url_l or "youtu.be" in url_l

    opts: Dict[str, Any] = {
        "quiet":          True,
        "no_warnings":    True,
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


def do_extract(url: str) -> dict:
    with yt_dlp.YoutubeDL(build_opts(url)) as ydl:
        info = ydl.extract_info(url, download=False)
        return ydl.sanitize_info(info)


def height_of(res: Optional[str]) -> int:
    if not res:
        return 0
    m = re.search(r"(\d+)x(\d+)", res)
    return int(m.group(2)) if m else 0


def parse_formats(raw: list, info: dict) -> Dict[str, list]:
    video_out  = []
    audio_out  = []
    silent_vid = []

    seen_v: set = set()
    seen_a: set = set()

    for f in raw:
        url = f.get("url") or ""
        if not url:
            continue

        proto = f.get("protocol") or ""
        if proto in ("m3u8", "m3u8_native", "dash", "rtsp"):
            continue
        if url.endswith(".m3u8"):
            continue

        vc = f.get("vcodec") or "none"
        ac = f.get("acodec") or "none"
        has_v = vc != "none"
        has_a = ac != "none"

        w = f.get("width")
        h = f.get("height")
        res = f.get("resolution") or (f"{w}x{h}" if w and h else None)

        fmt = {
            "format_id":   str(f.get("format_id", "")),
            "ext":         str(f.get("ext", "mp4")),
            "resolution":  res,
            "format_note": f.get("format_note"),
            "filesize":    f.get("filesize") or f.get("filesize_approx"),
            "url":         url,
            "vcodec":      vc,
            "acodec":      ac,
            "abr":         f.get("abr"),
            "tbr":         f.get("tbr"),
            "fps":         f.get("fps"),
            "has_audio":   has_a,
        }

        if has_v and has_a:
            key = res or fmt["format_id"]
            if key not in seen_v:
                seen_v.add(key)
                video_out.append(fmt)

        elif has_v and not has_a:
            key = res or fmt["format_id"]
            if key not in seen_v:
                seen_v.add(key)
                silent_vid.append(fmt)

        elif not has_v and has_a:
            abr = f.get("abr") or 0
            key = f"{abr}|{fmt['ext']}"
            if key not in seen_a:
                seen_a.add(key)
                audio_out.append(fmt)

    # No combined streams → use top-level url or silent as fallback
    if not video_out:
        top = info.get("url", "")
        if top and not top.endswith(".m3u8"):
            w = info.get("width")
            h = info.get("height")
            res = f"{w}x{h}" if w and h else "Best"
            video_out.append({
                "format_id":   "best",
                "ext":         info.get("ext", "mp4"),
                "resolution":  res,
                "format_note": "Best Quality",
                "filesize":    info.get("filesize"),
                "url":         top,
                "vcodec":      info.get("vcodec", "avc1"),
                "acodec":      info.get("acodec", "mp4a"),
                "abr":         info.get("abr"),
                "tbr":         info.get("tbr"),
                "fps":         info.get("fps"),
                "has_audio":   True,
            })
        else:
            video_out = silent_vid

    video_out.sort(key=lambda x: -height_of(x.get("resolution")))
    audio_out.sort(key=lambda x: -(x.get("abr") or 0))

    return {"video": video_out, "audio": audio_out}


@app.get("/")
async def root():
    return {"status": "ok", "message": "Video Extractor API running.", "version": "4.0.0"}


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
        raise HTTPException(400, "URL is required.")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Invalid URL.")

    logger.info(f"Extracting: {url}")

    try:
        info = do_extract(url)
    except yt_dlp.utils.DownloadError as e:
        err = str(e).lower()
        logger.warning(f"yt-dlp: {e}")
        if "unsupported url"  in err: raise HTTPException(422, "This URL is not supported.")
        if "private"          in err or "login" in err: raise HTTPException(422, "Private video or login required.")
        if "unavailable"      in err: raise HTTPException(422, "Video unavailable or removed.")
        if "429"              in err or "rate" in err: raise HTTPException(429, "Rate limited. Try again later.")
        raise HTTPException(422, f"Extraction failed: {str(e)[:200]}")
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        raise HTTPException(500, f"Server error: {str(e)[:150]}")

    raw = info.get("formats") or []
    if not raw and info.get("url"):
        raw = [info]

    parsed = parse_formats(raw, info)
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
