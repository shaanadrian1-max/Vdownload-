# Video Extractor API — Railway Deployment

## GitHub এ কীভাবে দেবে (Step by Step)

### ১. GitHub এ নতুন repository বানাও
- github.com → New repository
- Name: `video-extractor-api`
- Public করো
- Create repository

### ২. এই ZIP এর `railway-backend` ফোল্ডারের সব ফাইল upload করো
ফাইলগুলো হলো:
```
main.py
requirements.txt
apt.txt
Procfile
railway.toml
nixpacks.toml
runtime.txt
.gitignore
```

### ৩. Railway এ connect করো
- railway.app → New Project → Deploy from GitHub repo
- এই repo select করো
- Deploy হবে automatically

### ৪. Environment Variables (Railway → Variables)
```
ALLOWED_ORIGINS = *
```
Optional:
```
API_SECRET      = (যেকোনো secret key)
YTDLP_PROXY     = (proxy থাকলে)
```

### ৫. WordPress Plugin
`wp-plugin` ফোল্ডার টা zip করে WordPress এ install করো।
তারপর Settings → Video Downloader → Railway URL দাও।

## কী Fix করা হয়েছে
- Python 3.11 force করা (3.13 এ pydantic problem ছিল)
- YouTube android client দিয়ে extract (bot block bypass)
- Auto yt-dlp update on startup
- Timeout 120 সেকেন্ড করা
- Better error messages
- Health check endpoint `/health`
