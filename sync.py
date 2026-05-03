import json
import os
import sys
import tempfile
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
    if not cfg.get("youtube_channels") or not isinstance(cfg["youtube_channels"], list):
        sys.exit("Error: config.yaml missing required field 'youtube_channels' (must be a list of URLs)")
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


def fetch_metadata(url: str) -> dict:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return info


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


def download_audio(url: str, filename: str, output_dir: str) -> tuple[Path, dict]:
    stem = Path(filename).stem
    outtmpl = str(Path(output_dir) / stem)

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    output_path = Path(output_dir) / filename
    if not output_path.exists():
        raise FileNotFoundError(
            f"Expected output file not found: {output_path}. "
            "Check that ffmpeg is installed and on PATH."
        )
    return output_path, info


def _fetch_tab(base_url: str, tab: str, channel_url: str) -> list[dict]:
    fetch_url = f"{base_url}/{tab}"
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlistend": 3,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(fetch_url, download=False)
    except Exception as e:
        print(f"  WARNING: Failed to fetch {fetch_url}: {e}", file=sys.stderr)
        return []

    channel_name = (
        info.get("uploader") or info.get("channel") or
        info.get("title") or channel_url
    )
    videos = []
    for entry in info.get("entries") or []:
        if not entry or not entry.get("id"):
            continue
        videos.append({
            "video_id": entry["id"],
            "url": f"https://www.youtube.com/watch?v={entry['id']}",
            "title": entry.get("title") or f"Video {entry['id']}",
            "duration": entry.get("duration"),
            "upload_date": entry.get("upload_date"),
            "channel_url": channel_url,
            "channel_name": channel_name,
        })
    return videos


def fetch_channel_videos(channel_url: str, filter_type: str = "videos") -> list[dict]:
    base = channel_url.rstrip("/")
    for suffix in ("/videos", "/shorts", "/streams"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break

    if filter_type == "all":
        seen: dict[str, dict] = {}
        for tab in ("videos", "shorts"):
            for v in _fetch_tab(base, tab, channel_url):
                seen.setdefault(v["video_id"], v)
        return list(seen.values())

    return _fetch_tab(base, filter_type, channel_url)



def download_and_cache(video: dict, downloads_dir: Path, state: dict) -> dict | None:
    video_id = video["video_id"]
    dest = downloads_dir / make_filename(video_id)

    if dest.exists() and video_id in state:
        print(f"  Already cached: {dest.name}")
        return state[video_id]

    if dest.exists() and video_id not in state:
        print(f"  File exists, fetching metadata...")
        try:
            info = fetch_metadata(video["url"])
        except Exception as e:
            print(f"  WARNING: Failed to fetch metadata: {e}", file=sys.stderr)
            return None
        pub_dt = parse_publish_date(info.get("upload_date") or "")
        return {
            "url": video["url"],
            "filename": make_filename(video_id),
            "title": info.get("title") or video["title"],
            "description": (info.get("description") or "")[:2000],
            "duration": int(info.get("duration") or 0),
            "publish_date": pub_dt.isoformat(),
            "file_size": dest.stat().st_size,
            "channel_url": video["channel_url"],
            "channel_name": video["channel_name"],
        }

    print(f"  Downloading: {video['title']!r}")
    try:
        _, info = download_audio(video["url"], make_filename(video_id), str(downloads_dir))
    except Exception as e:
        print(f"  WARNING: Failed to download: {e}", file=sys.stderr)
        return None

    pub_dt = parse_publish_date(info.get("upload_date") or "")
    return {
        "url": video["url"],
        "filename": make_filename(video_id),
        "title": info.get("title") or video["title"],
        "description": (info.get("description") or "")[:2000],
        "duration": int(info.get("duration") or 0),
        "publish_date": pub_dt.isoformat(),
        "file_size": dest.stat().st_size,
        "channel_url": video["channel_url"],
        "channel_name": video["channel_name"],
    }


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

    cfg = load_config()
    state = load_state()

    downloads_dir = Path("downloads")
    downloads_dir.mkdir(exist_ok=True)

    filter_type = cfg.get("filter", "videos")

    # Fetch recent videos from all channels
    all_videos = []
    for channel_url in cfg["youtube_channels"]:
        print(f"Fetching videos from {channel_url}...")
        videos = fetch_channel_videos(channel_url, filter_type)
        print(f"  Found {len(videos)} video(s)")
        all_videos.extend(videos)

    if not all_videos:
        sys.exit("No videos found from any channel.")

    selected = all_videos
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
