import json
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import boto3
import yaml
from botocore.config import Config
from dotenv import load_dotenv
from feedgen.feed import FeedGenerator

import yt_dlp

STATE_FILE = Path("state.json")
CONFIG_FILE = Path("config.yaml")


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        sys.exit(f"Error: {CONFIG_FILE} not found.")
    with CONFIG_FILE.open() as f:
        cfg = yaml.safe_load(f)
    required = [
        ("podcast", "title"), ("podcast", "author"), ("podcast", "description"),
        ("podcast", "language"), ("r2", "bucket_name"), ("r2", "endpoint_url"),
        ("r2", "public_base_url"),
    ]
    for section, key in required:
        if not cfg.get(section, {}).get(key):
            sys.exit(f"Error: config.yaml missing required field '{section}.{key}'")
    cfg["r2"].setdefault("feed_filename", "feed.xml")
    channels = cfg.get("youtube_channels") or []
    videos = cfg.get("youtube_videos") or []
    playlists = cfg.get("youtube_playlists") or []
    if not isinstance(channels, list):
        sys.exit("Error: config.yaml 'youtube_channels' must be a list of URLs")
    if not isinstance(videos, list):
        sys.exit("Error: config.yaml 'youtube_videos' must be a list of URLs")
    if not isinstance(playlists, list):
        sys.exit("Error: config.yaml 'youtube_playlists' must be a list of URLs")
    if not channels and not videos and not playlists:
        sys.exit("Error: config.yaml must have at least one entry in 'youtube_channels', 'youtube_videos', or 'youtube_playlists'")
    cfg["youtube_channels"] = channels
    cfg["youtube_videos"] = videos
    cfg["youtube_playlists"] = playlists
    return cfg


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    with STATE_FILE.open() as f:
        raw = json.load(f)
    # Migrate old URL-keyed state to video_id-keyed state
    migrated = {}
    for key, entry in raw.items():
        if key.startswith("http"):
            video_id = Path(entry["filename"]).stem
            entry.setdefault("url", key)
            migrated[video_id] = entry
        else:
            migrated[key] = entry
    return migrated


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(state, f, indent=2, default=str)
    tmp.replace(STATE_FILE)


def make_filename(video_id: str) -> str:
    return f"{video_id}.mp3"


def parse_publish_date(upload_date: str) -> datetime:
    try:
        return datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return datetime.now(tz=timezone.utc)


def format_duration(seconds: int | float | None) -> str:
    if not seconds:
        return "?:??"
    s = int(seconds)
    if s < 3600:
        return f"{s // 60}:{s % 60:02d}"
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def download_audio(url: str, filename: str, output_dir: str) -> Path:
    stem = Path(filename).stem
    outtmpl = str(Path(output_dir) / stem)

    ydl_opts = {
        "format": "best[ext=mp4]/best",
        "outtmpl": outtmpl,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }
    tmp_cookies: Path | None = None
    if cookies_content := os.environ.get("YTDLP_COOKIES"):
        tmp_cookies = Path(tempfile.mktemp(suffix=".txt"))
        tmp_cookies.write_text(cookies_content)
        ydl_opts["cookiefile"] = str(tmp_cookies)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    finally:
        if tmp_cookies:
            tmp_cookies.unlink(missing_ok=True)

    output_path = Path(output_dir) / filename
    if not output_path.exists():
        raise FileNotFoundError(
            f"Expected output file not found: {output_path}. "
            "Check that ffmpeg is installed and on PATH."
        )
    return output_path


def _youtube_api_get(endpoint: str, params: dict) -> dict:
    url = "https://www.googleapis.com/youtube/v3/" + endpoint + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url) as resp:
        return json.loads(resp.read())


def _parse_iso_duration(duration: str) -> int:
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration or "")
    if not m:
        return 0
    return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)


def _resolve_uploads_playlist(channel_url: str, api_key: str) -> tuple[str, str]:
    url = channel_url.rstrip("/")
    if "/channel/" in url:
        params = {"part": "contentDetails", "id": url.split("/channel/")[-1].split("/")[0], "key": api_key}
    elif "/@" in url:
        params = {"part": "contentDetails", "forHandle": url.split("/@")[-1].split("/")[0], "key": api_key}
    elif "/user/" in url:
        params = {"part": "contentDetails", "forUsername": url.split("/user/")[-1].split("/")[0], "key": api_key}
    else:
        raise ValueError(f"Unsupported channel URL format: {channel_url}")

    data = _youtube_api_get("channels", params)
    items = data.get("items") or []
    if not items:
        raise ValueError(f"Channel not found: {channel_url}")
    channel_id = items[0]["id"]
    uploads_playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    return channel_id, uploads_playlist_id


def fetch_channel_videos(channel_url: str, api_key: str, max_results: int = 3) -> list[dict]:
    try:
        _, uploads_playlist_id = _resolve_uploads_playlist(channel_url, api_key)
    except Exception as e:
        print(f"  WARNING: Failed to resolve channel: {e}", file=sys.stderr)
        return []

    try:
        playlist_data = _youtube_api_get("playlistItems", {
            "part": "snippet",
            "playlistId": uploads_playlist_id,
            "maxResults": max_results,
            "key": api_key,
        })
    except Exception as e:
        print(f"  WARNING: Failed to fetch playlist: {e}", file=sys.stderr)
        return []

    items = playlist_data.get("items") or []
    if not items:
        return []

    video_ids = [item["snippet"]["resourceId"]["videoId"] for item in items]

    try:
        videos_data = _youtube_api_get("videos", {
            "part": "snippet,contentDetails",
            "id": ",".join(video_ids),
            "key": api_key,
        })
    except Exception as e:
        print(f"  WARNING: Failed to fetch video details: {e}", file=sys.stderr)
        return []

    videos = []
    for item in videos_data.get("items") or []:
        video_id = item["id"]
        snippet = item["snippet"]
        published_at = snippet.get("publishedAt", "")
        upload_date = published_at[:10].replace("-", "") if published_at else ""
        videos.append({
            "video_id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "title": snippet.get("title") or f"Video {video_id}",
            "description": (snippet.get("description") or "")[:2000],
            "duration": _parse_iso_duration(item["contentDetails"].get("duration", "")),
            "upload_date": upload_date,
            "channel_url": channel_url,
            "channel_name": snippet.get("channelTitle") or channel_url,
        })
    return videos


def _extract_video_id(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname in ("youtu.be",):
        return parsed.path.lstrip("/").split("/")[0] or None
    if parsed.hostname in ("www.youtube.com", "youtube.com", "m.youtube.com"):
        if parsed.path.startswith("/shorts/"):
            return parsed.path.split("/shorts/")[1].split("/")[0] or None
        qs = urllib.parse.parse_qs(parsed.query)
        return (qs.get("v") or [None])[0]
    return None


def fetch_playlist_videos(playlist_url: str, api_key: str) -> list[dict]:
    parsed = urllib.parse.urlparse(playlist_url)
    playlist_id = urllib.parse.parse_qs(parsed.query).get("list", [None])[0]
    if not playlist_id:
        print(f"  WARNING: Could not extract playlist ID from URL: {playlist_url}", file=sys.stderr)
        return []

    items = []
    page_token = None
    while True:
        params = {
            "part": "snippet",
            "playlistId": playlist_id,
            "maxResults": 50,
            "key": api_key,
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            data = _youtube_api_get("playlistItems", params)
        except Exception as e:
            print(f"  WARNING: Failed to fetch playlist items: {e}", file=sys.stderr)
            break
        items.extend(data.get("items") or [])
        page_token = data.get("nextPageToken")
        if not page_token:
            break

    if not items:
        return []

    videos = []
    # Fetch video details in batches of 50
    video_ids = [item["snippet"]["resourceId"]["videoId"] for item in items]
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        try:
            details = _youtube_api_get("videos", {
                "part": "snippet,contentDetails",
                "id": ",".join(batch),
                "key": api_key,
            })
        except Exception as e:
            print(f"  WARNING: Failed to fetch video details for batch: {e}", file=sys.stderr)
            continue
        for item in details.get("items") or []:
            video_id = item["id"]
            snippet = item["snippet"]
            published_at = snippet.get("publishedAt", "")
            upload_date = published_at[:10].replace("-", "") if published_at else ""
            videos.append({
                "video_id": video_id,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "title": snippet.get("title") or f"Video {video_id}",
                "description": (snippet.get("description") or "")[:2000],
                "duration": _parse_iso_duration(item["contentDetails"].get("duration", "")),
                "upload_date": upload_date,
                "channel_url": playlist_url,
                "channel_name": snippet.get("channelTitle") or playlist_url,
            })
    return videos


def fetch_video_details(video_url: str, api_key: str) -> dict | None:
    video_id = _extract_video_id(video_url)
    if not video_id:
        print(f"  WARNING: Could not extract video ID from URL: {video_url}", file=sys.stderr)
        return None
    try:
        data = _youtube_api_get("videos", {
            "part": "snippet,contentDetails",
            "id": video_id,
            "key": api_key,
        })
    except Exception as e:
        print(f"  WARNING: Failed to fetch video details for {video_url}: {e}", file=sys.stderr)
        return None
    items = data.get("items") or []
    if not items:
        print(f"  WARNING: Video not found: {video_url}", file=sys.stderr)
        return None
    item = items[0]
    snippet = item["snippet"]
    published_at = snippet.get("publishedAt", "")
    upload_date = published_at[:10].replace("-", "") if published_at else ""
    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": snippet.get("title") or f"Video {video_id}",
        "description": (snippet.get("description") or "")[:2000],
        "duration": _parse_iso_duration(item["contentDetails"].get("duration", "")),
        "upload_date": upload_date,
        "channel_url": video_url,
        "channel_name": snippet.get("channelTitle") or video_url,
    }



def download_and_cache(video: dict, downloads_dir: Path, state: dict) -> dict | None:
    video_id = video["video_id"]
    dest = downloads_dir / make_filename(video_id)

    if dest.exists() and video_id in state:
        print(f"  Already cached: {dest.name}")
        return state[video_id]

    pub_dt = parse_publish_date(video.get("upload_date") or "")
    entry = {
        "url": video["url"],
        "filename": make_filename(video_id),
        "title": video["title"],
        "description": video.get("description") or "",
        "duration": int(video.get("duration") or 0),
        "publish_date": pub_dt.isoformat(),
        "channel_url": video["channel_url"],
        "channel_name": video["channel_name"],
    }

    if dest.exists() and video_id not in state:
        print(f"  File exists, using API metadata.")
        entry["file_size"] = dest.stat().st_size
        return entry

    print(f"  Downloading: {video['title']!r}")
    try:
        download_audio(video["url"], make_filename(video_id), str(downloads_dir))
    except Exception as e:
        print(f"  WARNING: Failed to download: {e}", file=sys.stderr)
        return None

    entry["file_size"] = dest.stat().st_size
    return entry


def make_r2_client(cfg: dict):
    return boto3.client(
        "s3",
        endpoint_url=cfg["r2"]["endpoint_url"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def file_exists_in_r2(client, bucket: str, key: str) -> bool:
    from botocore.exceptions import ClientError
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError:
        return False


def upload_file(client, bucket: str, local_path: Path, key: str) -> int:
    suffix = local_path.suffix.lower()
    content_type = {
        ".mp3": "audio/mpeg",
        ".xml": "application/xml",
    }.get(suffix, "application/octet-stream")

    file_size = local_path.stat().st_size
    with local_path.open("rb") as f:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=f,
            ContentType=content_type,
        )
    return file_size



def build_feed(cfg: dict, state: dict) -> bytes:
    pc = cfg["podcast"]
    r2 = cfg["r2"]
    feed_url = f"{r2['public_base_url'].rstrip('/')}/{r2['feed_filename']}"

    fg = FeedGenerator()
    fg.load_extension("podcast")

    fg.id(feed_url)
    fg.title(pc["title"])
    fg.author({"name": pc["author"], "email": pc.get("email") or ""})
    fg.description(pc["description"])
    fg.language(pc["language"])
    fg.link(href=feed_url, rel="self")
    fg.link(href=feed_url, rel="alternate")
    fg.podcast.itunes_author(pc["author"])
    fg.podcast.itunes_explicit("no")
    if pc.get("image_url"):
        fg.podcast.itunes_image(pc["image_url"])
        fg.image(url=pc["image_url"], title=pc["title"])

    processed = []
    for video_id, entry in state.items():
        pub = datetime.fromisoformat(entry["publish_date"])
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        processed.append((pub, video_id, entry))
    processed.sort(key=lambda x: x[0], reverse=True)

    for pub_dt, video_id, entry in processed:
        mp3_url = f"{r2['public_base_url'].rstrip('/')}/{entry['filename']}"
        fe = fg.add_entry()
        fe.id(entry.get("url") or f"https://www.youtube.com/watch?v={video_id}")
        fe.title(entry["title"])
        fe.description(entry.get("description") or entry["title"])
        fe.published(pub_dt)
        fe.updated(pub_dt)
        fe.enclosure(
            url=mp3_url,
            length=str(entry["file_size"]),
            type="audio/mpeg",
        )
        fe.podcast.itunes_duration(str(entry["duration"]))
        fe.podcast.itunes_author(pc["author"])

    return fg.rss_str(pretty=True)


def main() -> None:
    load_dotenv()

    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        sys.exit("Error: YOUTUBE_API_KEY environment variable not set.")

    cfg = load_config()
    state = load_state()

    downloads_dir = Path("downloads")
    downloads_dir.mkdir(exist_ok=True)

    # Fetch recent videos from all channels
    all_videos = []
    for channel_url in cfg["youtube_channels"]:
        print(f"Fetching videos from {channel_url}...")
        videos = fetch_channel_videos(channel_url, api_key)
        print(f"  Found {len(videos)} video(s)")
        all_videos.extend(videos)

    # Fetch videos from user-curated playlists (dynamic source)
    for playlist_url in cfg["youtube_playlists"]:
        print(f"Fetching playlist {playlist_url}...")
        videos = fetch_playlist_videos(playlist_url, api_key)
        print(f"  Found {len(videos)} video(s)")
        all_videos.extend(videos)

    # Fetch individually listed videos
    for video_url in cfg["youtube_videos"]:
        print(f"Fetching video {video_url}...")
        video = fetch_video_details(video_url, api_key)
        if video:
            print(f"  Found: {video['title']!r}")
            all_videos.append(video)

    if not all_videos:
        sys.exit("No videos found from any channel or individual video URL.")

    # Filter out videos shorter than 2 minutes (120 seconds)
    MIN_DURATION_SECONDS = 120
    selected = [v for v in all_videos if v.get("duration", 0) >= MIN_DURATION_SECONDS]
    skipped_short = len(all_videos) - len(selected)

    if skipped_short > 0:
        print(f"Skipped {skipped_short} video(s) shorter than 2 minutes")

    print(f"\n{len(selected)} episode(s) to process.")

    # Fail fast on R2 credentials before any downloading
    try:
        r2_client = make_r2_client(cfg)
    except KeyError as e:
        sys.exit(f"Error: Missing environment variable {e}. Check your .env file.")

    # Download any not yet cached
    for video in list(selected):
        print(f"\nProcessing: {video['title']!r}")
        entry = download_and_cache(video, downloads_dir, state)
        if entry is None:
            print("  Skipped (see warning above).")
            selected = [v for v in selected if v["video_id"] != video["video_id"]]
            continue
        state[video["video_id"]] = entry
        save_state(state)

    if not selected:
        sys.exit("No episodes successfully processed.")

    feed_state = {
        v["video_id"]: state[v["video_id"]]
        for v in selected
        if v["video_id"] in state
    }

    print(f"\nUploading {len(feed_state)} episode(s) to R2...")
    uploaded = 0
    skipped = 0
    for video_id, entry in feed_state.items():
        local_path = downloads_dir / entry["filename"]
        key = entry["filename"]
        if file_exists_in_r2(r2_client, cfg["r2"]["bucket_name"], key):
            print(f"  {key} (already in R2, skipping)")
            skipped += 1
        else:
            print(f"  {key}...")
            upload_file(r2_client, cfg["r2"]["bucket_name"], local_path, key)
            uploaded += 1
    print(f"  {uploaded} uploaded, {skipped} skipped.")

    # Generate and upload feed
    print("\nUploading feed.xml...")
    feed_bytes = build_feed(cfg, feed_state)
    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tmp:
        tmp.write(feed_bytes)
        tmp_path = Path(tmp.name)
    try:
        upload_file(r2_client, cfg["r2"]["bucket_name"], tmp_path, cfg["r2"]["feed_filename"])
    finally:
        tmp_path.unlink(missing_ok=True)

    feed_url = f"{cfg['r2']['public_base_url'].rstrip('/')}/{cfg['r2']['feed_filename']}"
    print(f"\nDone. Feed URL: {feed_url}")


if __name__ == "__main__":
    main()
