# youtube2podcast

Converts YouTube channels into a podcast feed hosted on Cloudflare R2.

## Requirements

- Python 3.10+
- [uv](https://astral.sh/uv) — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- ffmpeg — `brew install ffmpeg`

## Setup

1. Install dependencies:
   ```bash
   make setup
   ```

2. Copy `.env.example` to `.env` and fill in your credentials:
   ```
   R2_ACCESS_KEY_ID=...
   R2_SECRET_ACCESS_KEY=...
   YOUTUBE_API_KEY=...
   ```
   - R2 credentials: **Cloudflare Dashboard → R2 → Manage R2 API Tokens**
   - YouTube API key: **[Google Cloud Console](https://console.cloud.google.com) → APIs & Services → Credentials → Create API key**, with YouTube Data API v3 enabled

3. Edit `config.yaml`:
   - Fill in your podcast metadata (`title`, `author`, `description`)
   - Set `endpoint_url` — found in **R2 → your bucket → Settings**
   - Set `public_base_url` — the R2.dev public URL from **R2 → your bucket → Settings → Public access**
   - Add your YouTube channels:
     ```yaml
     youtube_channels:
       - https://www.youtube.com/@channelhandle
     ```

## Usage

```bash
make sync
```

This will:
1. Fetch the 3 most recent videos from each configured channel via the YouTube Data API
2. Download any new videos as MP3 (already-downloaded episodes are cached in `downloads/`)
3. Upload new MP3s to R2 and regenerate `feed.xml`
4. Print the feed URL

Paste the feed URL into Apple Podcasts via **File → Add Show by URL**.
