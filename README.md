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

2. Copy `.env.example` to `.env` and fill in your Cloudflare R2 API credentials:
   ```
   R2_ACCESS_KEY_ID=...
   R2_SECRET_ACCESS_KEY=...
   ```
   Get these from **Cloudflare Dashboard → R2 → Manage R2 API Tokens**.

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
1. Fetch the 10 most recent videos from each configured channel
2. Show an interactive checklist — select the episodes you want
3. Download any new selections as MP3 (already-downloaded episodes are cached in `downloads/`)
4. Replace all MP3s on R2 with the current selection and regenerate `feed.xml`
5. Print the feed URL

Paste the feed URL into Apple Podcasts via **File → Add Show by URL**.
