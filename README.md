# youtube2podcast

Converts YouTube videos into a podcast feed hosted on Cloudflare R2.

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

3. Edit `config.yaml` with your podcast metadata and R2 bucket details:
   - `endpoint_url`: found in **R2 → your bucket → Settings**
   - `public_base_url`: the R2.dev public URL from **R2 → your bucket → Settings → Public access**

## Usage

Add YouTube URLs to `episodes.yaml`:

```yaml
episodes:
  - url: https://www.youtube.com/watch?v=EXAMPLE
```

Then sync:

```bash
make sync
```

This downloads each new episode as MP3, uploads it to R2, and regenerates `feed.xml`.
The feed URL is printed at the end — paste it into Apple Podcasts via **File → Add Show by URL**.

### Test locally (no R2 needed)

```bash
make download
```

Downloads MP3s to `downloads/` without uploading anything.
