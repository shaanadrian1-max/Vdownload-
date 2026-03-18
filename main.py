import os, re, logging, tempfile, shutil
from typing import Optional, Dict, Any, List
from pathlib import Path
from urllib.parse import quote

import yt_dlp
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Video Extractor", version="7.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

API_SECRET = os.getenv("API_SECRET", "")
BASE_URL   = os.getenv("API_BASE_URL", "https://web-production-6fd4b.up.railway.app")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

def check_secret(s):
    if API_SECRET and s != API_SECRET:
        raise HTTPException(403, "Forbidden.")

def enc(s): return quote(str(s), safe="")
def is_yt(u): return "youtube.com" in u or "youtu.be" in u
def is_fb(u): return "facebook.com" in u or "fb.watch" in u
def is_tt(u): return "tiktok.com" in u
def h_of(res):
    if not res: return 0
    m = re.search(r"(\d+)x(\d+)", str(res))
    return int(m.group(2)) if m else 0

def base_url():
    d = os.getenv("RAILWAY_PUBLIC_DOMAIN","")
    return f"https://{d}" if d else BASE_URL

def dl_link(orig, fmt, ext, label):
    return f"{base_url()}/dl?url={enc(orig)}&fmt={enc(fmt)}&ext={enc(ext)}&label={enc(label)}"


# ─────────────────────────────────────────────────────────────────────────────
# EXTRACT opts
# ─────────────────────────────────────────────────────────────────────────────
def extract_opts(url: str) -> dict:
    opts: Dict[str,Any] = {
        "quiet": True, "no_warnings": True,
        "skip_download": True, "noplaylist": True,
        "geo_bypass": True, "socket_timeout": 30,
        "http_headers": HEADERS,
    }

    if is_yt(url):
        # Use tv_embedded + mweb clients — avoids "Sign in to confirm not a bot"
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["tv_embedded", "ios", "android"],
                "player_skip": ["webpage", "configs"],
            }
        }
        cf = os.getenv("YTDLP_COOKIES_FILE","")
        if cf and os.path.isfile(cf): opts["cookiefile"] = cf

    if proxy := os.getenv("YTDLP_PROXY",""):
        opts["proxy"] = proxy

    return opts


def get_info(url: str) -> dict:
    with yt_dlp.YoutubeDL(extract_opts(url)) as ydl:
        info = ydl.extract_info(url, download=False)
        return ydl.sanitize_info(info)


# ─────────────────────────────────────────────────────────────────────────────
# FORMAT PARSING
# ─────────────────────────────────────────────────────────────────────────────
def parse_formats(raw: List[dict], info: dict) -> dict:
    orig = info.get("webpage_url") or info.get("original_url") or ""
    url_l = orig.lower()

    video_out: list = []
    audio_out: list = []
    seen_v: set = set()
    seen_a: set = set()

    for f in raw:
        if not f.get("url"): continue
        proto = f.get("protocol","")
        if proto in ("m3u8","m3u8_native","dash","rtsp"): continue
        if str(f.get("url","")).endswith(".m3u8"): continue

        vc  = f.get("vcodec","none") or "none"
        ac  = f.get("acodec","none") or "none"
        has_v = vc != "none"
        has_a = ac != "none"

        w   = f.get("width");  h = f.get("height")
        res = f.get("resolution") or (f"{w}x{h}" if w and h else None)
        ext = str(f.get("ext") or "mp4")
        fid = str(f.get("format_id") or "best")
        note= f.get("format_note") or ""
        abr = f.get("abr") or 0

        if has_v and has_a:
            key = res or fid
            if key not in seen_v:
                seen_v.add(key)
                label = res or note or fid
                video_out.append({
                    "format_id": fid, "ext": ext, "resolution": res,
                    "format_note": note, "filesize": f.get("filesize") or f.get("filesize_approx"),
                    "url": dl_link(orig, fid, ext, label),
                    "vcodec": vc, "acodec": ac, "abr": abr, "type": "video",
                })

        elif not has_v and has_a:
            key = f"{abr:.0f}|{ext}"
            if key not in seen_a:
                seen_a.add(key)
                abr_label = f"{abr:.0f}kbps" if abr else note or ext
                audio_out.append({
                    "format_id": fid, "ext": ext, "resolution": None,
                    "format_note": note or abr_label, "filesize": f.get("filesize") or f.get("filesize_approx"),
                    "url": dl_link(orig, fid, ext, abr_label),
                    "vcodec": "none", "acodec": ac, "abr": abr, "type": "audio",
                })
        # video-only (no audio) — skip for display, handled below

    # ── YouTube: explicit quality selectors with H.264 ────────────────────────
    if is_yt(url_l):
        # Format selector: H.264 video + best audio, merged to mp4
        yt_quals = [
            ("bestvideo[vcodec^=avc1][height>=2160]+bestaudio[ext=m4a]/bestvideo[height>=2160]+bestaudio/best[height>=2160]", "mp4", "3840x2160", "2160p 4K"),
            ("bestvideo[vcodec^=avc1][height>=1080]+bestaudio[ext=m4a]/bestvideo[height>=1080]+bestaudio/best[height>=1080]", "mp4", "1920x1080", "1080p"),
            ("bestvideo[vcodec^=avc1][height>=720]+bestaudio[ext=m4a]/bestvideo[height>=720]+bestaudio/best[height>=720]",    "mp4", "1280x720",  "720p"),
            ("bestvideo[vcodec^=avc1][height>=480]+bestaudio[ext=m4a]/bestvideo[height>=480]+bestaudio/best[height>=480]",   "mp4", "854x480",   "480p"),
            ("bestvideo[vcodec^=avc1][height>=360]+bestaudio[ext=m4a]/bestvideo[height>=360]+bestaudio/best[height>=360]",   "mp4", "640x360",   "360p"),
        ]
        existing_h = {h_of(v.get("resolution")) for v in video_out}
        for fmt_sel, ext, res, label in yt_quals:
            if h_of(res) not in existing_h:
                video_out.append({
                    "format_id": fmt_sel, "ext": ext, "resolution": res,
                    "format_note": label, "filesize": None,
                    "url": dl_link(orig, fmt_sel, ext, label),
                    "vcodec": "avc1", "acodec": "mp4a", "abr": None, "type": "video",
                })
        if not audio_out:
            audio_out.append({
                "format_id": "bestaudio[ext=m4a]/bestaudio", "ext": "m4a",
                "resolution": None, "format_note": "Best Audio (AAC)",
                "filesize": None,
                "url": dl_link(orig, "bestaudio[ext=m4a]/bestaudio", "m4a", "Best Audio"),
                "vcodec": "none", "acodec": "aac", "abr": None, "type": "audio",
            })

    # ── Fallback ──────────────────────────────────────────────────────────────
    if not video_out:
        top = info.get("url","")
        if top and not top.endswith(".m3u8"):
            w = info.get("width"); h = info.get("height")
            res = f"{w}x{h}" if w and h else "Best"
            video_out.append({
                "format_id": "best", "ext": info.get("ext","mp4"),
                "resolution": res, "format_note": "Best Quality", "filesize": None,
                "url": dl_link(orig, "best", info.get("ext","mp4"), "Best"),
                "vcodec": "avc1", "acodec": "mp4a", "abr": None, "type": "video",
            })

    video_out.sort(key=lambda x: -h_of(x.get("resolution")))
    audio_out.sort(key=lambda x: -(x.get("abr") or 0))
    return {"video": video_out, "audio": audio_out}


# ─────────────────────────────────────────────────────────────────────────────
# /dl — download proxy
# ─────────────────────────────────────────────────────────────────────────────
def build_dl_opts(url: str, fmt: str, out_tmpl: str) -> dict:
    opts: Dict[str,Any] = {
        "quiet": True, "no_warnings": True,
        "noplaylist": True, "socket_timeout": 120,
        "format": fmt,
        "outtmpl": out_tmpl,
        "http_headers": HEADERS,
        # Force H.264 output — compatible with ALL devices/browsers
        "merge_output_format": "mp4",
        "postprocessors": [
            {
                "key": "FFmpegVideoRemuxer",
                "preferedformat": "mp4",
            }
        ],
    }

    if is_yt(url):
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["tv_embedded", "ios", "android"],
                "player_skip": ["webpage", "configs"],
            }
        }
        cf = os.getenv("YTDLP_COOKIES_FILE","")
        if cf and os.path.isfile(cf): opts["cookiefile"] = cf

    elif is_fb(url):
        # Facebook: always use best combined to avoid expired format IDs
        if not (fmt.startswith("bestvideo") or fmt.startswith("bestaudio") or fmt == "best"):
            opts["format"] = "best"

    if proxy := os.getenv("YTDLP_PROXY",""):
        opts["proxy"] = proxy

    return opts


@app.get("/dl")
async def download_proxy(url: str, fmt: str = "best", ext: str = "mp4", label: str = "video"):
    if not url or not url.startswith(("http://","https://")):
        raise HTTPException(400, "Invalid URL.")
    logger.info(f"DL fmt={fmt[:50]} url={url[:60]}")

    tmp = tempfile.mkdtemp()
    out = os.path.join(tmp, "%(title).60s.%(ext)s")

    try:
        opts = build_dl_opts(url, fmt, out)
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:
        logger.warning(f"DL attempt 1 failed ({e}), retrying with best")
        try:
            opts["format"] = "best"
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except Exception as e2:
            shutil.rmtree(tmp, ignore_errors=True)
            raise HTTPException(500, f"Download failed: {str(e2)[:150]}")

    files = list(Path(tmp).glob("*"))
    if not files:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(500, "File not found after download.")

    out_file   = str(max(files, key=lambda p: p.stat().st_size))
    actual_ext = Path(out_file).suffix.lstrip(".") or ext
    clean      = re.sub(r'[^\w\s\-]','',label).strip()[:50] or "video"
    filename   = f"{clean}.{actual_ext}"

    is_audio_dl = (not is_yt(url) and "audio" in label.lower()) or actual_ext in ("m4a","mp3","aac","opus")
    mime = "audio/mp4" if is_audio_dl and actual_ext == "m4a" else \
           "audio/mpeg" if actual_ext == "mp3" else "video/mp4"

    def stream():
        try:
            with open(out_file,"rb") as f:
                while chunk := f.read(65536):
                    yield chunk
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    return StreamingResponse(stream(), media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"status":"ok","message":"Video Extractor API v7.0 running."}

@app.get("/health")
async def health():
    return {"status":"ok"}


@app.post("/extract")
async def extract(request: dict, x_api_secret: Optional[str] = Header(default=None)):
    check_secret(x_api_secret)
    url = (request.get("url") or "").strip()
    if not url: raise HTTPException(400,"URL is required.")
    if not url.startswith(("http://","https://")): raise HTTPException(400,"Invalid URL.")

    logger.info(f"Extracting: {url}")
    try:
        info = get_info(url)
    except yt_dlp.utils.DownloadError as e:
        err = str(e).lower()
        if "unsupported url"  in err: raise HTTPException(422,"This URL is not supported.")
        if "sign in"          in err or "bot" in err: raise HTTPException(422,"YouTube requires verification. Try another video or use cookies.")
        if "private"          in err or "login" in err: raise HTTPException(422,"Private video — login required.")
        if "unavailable"      in err: raise HTTPException(422,"Video unavailable or removed.")
        if "429"              in err: raise HTTPException(429,"Rate limited. Try again in a few minutes.")
        raise HTTPException(422, f"Extraction failed: {str(e)[:200]}")
    except Exception as e:
        logger.error(f"{e}", exc_info=True)
        raise HTTPException(500,f"Server error: {str(e)[:150]}")

    raw = info.get("formats") or []
    if not raw and info.get("url"): raw = [info]
    parsed = parse_formats(raw, info)
    vf, af = parsed["video"], parsed["audio"]
    if not vf and not af: raise HTTPException(422,"No downloadable formats found.")

    return {
        "title":     info.get("title") or "Untitled",
        "thumbnail": info.get("thumbnail"),
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "formats":   vf + af,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT",8000)))
