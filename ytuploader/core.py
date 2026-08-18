"""Core upload logic: OAuth, remux, resumable upload, state, verification.

Shared by the command-line interface (cli.py) and the web GUI (server.py).
"""

from __future__ import annotations

import csv
import json
import os
import random
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Iterable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]

RETRIABLE_STATUS = {500, 502, 503, 504}
MAX_RETRIES = 10
CHUNK = 8 * 1024 * 1024

# YouTube rejects these containers; we re-wrap them to .mp4 losslessly first.
NEEDS_REMUX = {".mkv", ".ts", ".m2ts", ".mts", ".ogv"}
VIDEO_EXTS = NEEDS_REMUX | {".mp4", ".mov", ".m4v", ".avi", ".wmv", ".flv", ".webm", ".mpg", ".mpeg"}

CATEGORIES = {
    "film": "1", "autos": "2", "music": "10", "pets": "15", "sports": "17",
    "travel": "19", "gaming": "20", "people": "22", "comedy": "23",
    "entertainment": "24", "news": "25", "howto": "26", "education": "27",
    "science": "28", "nonprofit": "29",
}

# Conditions that mean "stop for today" rather than "this file is broken".
_STOP_MARKERS = (
    "uploadlimitexceeded",
    "number of videos they may upload",
    "quotaexceeded",
    "dailylimitexceeded",
    "ratelimitexceeded",
)


class StopForToday(Exception):
    """Raised when the channel or API daily limit is reached."""


def stop_reason(e: HttpError) -> str | None:
    try:
        body = e.content.decode("utf-8", "ignore").lower()
    except Exception:
        body = ""
    if "uploadlimitexceeded" in body or "number of videos they may upload" in body:
        return "channel daily upload limit"
    if any(m in body for m in _STOP_MARKERS):
        return "API quota"
    if e.resp.status == 403 and "quota" in body:
        return "API quota"
    return None


# --------------------------------------------------------------------------- auth

def get_service(secrets: Path, token: Path):
    creds = None
    if token.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token), SCOPES)
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
        if not creds:
            if not secrets.exists():
                raise FileNotFoundError(f"OAuth client file not found: {secrets}")
            flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
            creds = flow.run_local_server(port=0, prompt="consent")
        token.write_text(creds.to_json())
        os.chmod(token, 0o600)

    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def has_token(token: Path) -> bool:
    return token.exists() and token.stat().st_size > 0


# --------------------------------------------------------------------------- helpers

def resolve_category(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return "22"
    if value.isdigit():
        return value
    key = value.lower()
    if key not in CATEGORIES:
        raise ValueError(f"Unknown category '{value}'")
    return CATEGORIES[key]


def norm_name(s: str) -> str:
    """Normalise a filename for fuzzy matching (NFC, strip zero-width chars)."""
    s = unicodedata.normalize("NFC", s)
    s = re.sub(r"[​-‏⁠﻿ ?]", "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def remux_to_mp4(src: Path, out_dir: Path, log: Callable[[str], None] = print) -> Path:
    """Losslessly re-wrap src into .mp4 (container swap, no re-encode)."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found — install it with: brew install ffmpeg")
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / (src.stem + ".mp4")
    if dest.exists() and dest.stat().st_size > 0:
        log(f"reusing existing {dest.name}")
        return dest
    base = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(src),
            "-map", "0:v:0", "-map", "0:a?", "-movflags", "+faststart"]
    r = subprocess.run(base + ["-c", "copy", str(dest)], capture_output=True, text=True)
    if r.returncode != 0:
        dest.unlink(missing_ok=True)
        log("stream copy failed, re-encoding audio to AAC")
        r = subprocess.run(
            base + ["-c:v", "copy", "-c:a", "aac", "-b:a", "320k", str(dest)],
            capture_output=True, text=True)
        if r.returncode != 0:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg failed: {r.stderr.strip()[-400:]}")
    return dest


# --------------------------------------------------------------------------- state

class State:
    """Append-only record of successful uploads, keyed by source file path."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.data: dict = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text())
            except Exception:
                self.data = {}

    def done(self, key: str) -> bool:
        return key in self.data

    def record(self, key: str, video_id: str, title: str) -> None:
        with self._lock:
            self.data[key] = {
                "video_id": video_id,
                "url": f"https://youtu.be/{video_id}",
                "title": title,
            }
            self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False))

    def drop(self, keys: Iterable[str]) -> int:
        with self._lock:
            n = 0
            for k in keys:
                if self.data.pop(k, None) is not None:
                    n += 1
            self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False))
            return n


# --------------------------------------------------------------------------- items

@dataclass
class Item:
    file: str
    title: str = ""
    description: str = ""
    tags: str = ""
    category: str = "22"
    privacy: str = "private"
    publish_at: str = ""
    playlist: str = ""
    thumbnail: str = ""
    made_for_kids: str = ""
    # runtime
    status: str = "pending"      # pending | uploading | done | failed | skipped
    progress: int = 0
    message: str = ""
    video_id: str = ""

    def dict(self) -> dict:
        return asdict(self)


def items_from_csv(csv_path: Path, base_dir: Path | None = None) -> list[Item]:
    base = base_dir or csv_path.parent
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if "file" not in (reader.fieldnames or []):
            raise ValueError("CSV must have a 'file' column")
        items = []
        for row in reader:
            row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
            if not row.get("file"):
                continue
            p = Path(row["file"]).expanduser()
            if not p.is_absolute():
                p = base / p
            row["file"] = str(p)
            items.append(Item(**{k: v for k, v in row.items() if k in Item.__annotations__}))
        return items


def items_from_folder(folder: Path) -> list[Item]:
    out = []
    for p in sorted(folder.iterdir()):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            out.append(Item(file=str(p), title=p.stem))
    return out


# --------------------------------------------------------------------------- upload

def upload_one(youtube, item: Item, remux_dir: Path | None = None,
               keep_temp: bool = False,
               on_progress: Callable[[int], None] = lambda p: None,
               log: Callable[[str], None] = print) -> str:
    source = Path(item.file)
    if not source.exists():
        raise FileNotFoundError(source)

    path = source
    temp_made = False
    if source.suffix.lower() in NEEDS_REMUX:
        target = remux_dir or Path(tempfile.gettempdir()) / "ppu_remux"
        log(f"converting {source.suffix} → .mp4 (lossless container swap)")
        path = remux_to_mp4(source, target, log)
        temp_made = remux_dir is None

    status = {
        "privacyStatus": item.privacy or "private",
        "selfDeclaredMadeForKids": str(item.made_for_kids).strip().lower()
        in {"1", "true", "yes"},
    }
    if item.publish_at.strip():
        status["privacyStatus"] = "private"
        status["publishAt"] = item.publish_at.strip()

    body = {
        "snippet": {
            "title": (item.title or source.stem)[:100],
            "description": item.description or "",
            "tags": [t.strip() for t in item.tags.split(",") if t.strip()],
            "categoryId": resolve_category(item.category),
        },
        "status": status,
    }

    media = MediaFileUpload(str(path), chunksize=CHUNK, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    log(f"uploading {path.name} ({path.stat().st_size / 1e6:.0f} MB)")
    response, attempt = None, 0
    try:
        while response is None:
            try:
                progress, response = request.next_chunk()
                if progress:
                    on_progress(int(progress.progress() * 100))
            except HttpError as e:
                if stop_reason(e):
                    raise
                if e.resp.status in RETRIABLE_STATUS:
                    attempt += 1
                    if attempt > MAX_RETRIES:
                        raise
                    delay = min(60, 2 ** attempt + random.random())
                    log(f"transient {e.resp.status}, retrying in {delay:.0f}s")
                    time.sleep(delay)
                else:
                    raise
            except (OSError, IOError) as e:
                attempt += 1
                if attempt > MAX_RETRIES:
                    raise
                delay = min(60, 2 ** attempt + random.random())
                log(f"network error ({e}), retrying in {delay:.0f}s")
                time.sleep(delay)
    finally:
        try:
            media.stream().close()
        except Exception:
            pass
        if temp_made and not keep_temp and path != source:
            path.unlink(missing_ok=True)

    video_id = response["id"]
    on_progress(100)
    log(f"done → https://youtu.be/{video_id}")

    if item.thumbnail.strip():
        tp = Path(item.thumbnail)
        if not tp.is_absolute():
            tp = source.parent / tp
        if tp.exists():
            try:
                youtube.thumbnails().set(
                    videoId=video_id, media_body=MediaFileUpload(str(tp))).execute()
                log("thumbnail set")
            except HttpError as e:
                log(f"thumbnail failed: {e}")

    if item.playlist.strip():
        try:
            add_to_playlist(youtube, video_id, item.playlist.strip())
            log(f"added to playlist {item.playlist}")
        except HttpError as e:
            log(f"playlist failed: {e}")

    return video_id


def add_to_playlist(youtube, video_id: str, playlist: str) -> None:
    playlist_id = playlist if playlist[:2] in ("PL", "UU") else None
    if playlist_id is None:
        res = youtube.playlists().list(part="snippet", mine=True, maxResults=50).execute()
        for it in res.get("items", []):
            if it["snippet"]["title"].strip().lower() == playlist.strip().lower():
                playlist_id = it["id"]
                break
        if playlist_id is None:
            created = youtube.playlists().insert(
                part="snippet,status",
                body={"snippet": {"title": playlist},
                      "status": {"privacyStatus": "private"}}).execute()
            playlist_id = created["id"]
    youtube.playlistItems().insert(
        part="snippet",
        body={"snippet": {"playlistId": playlist_id,
                          "resourceId": {"kind": "youtube#video", "videoId": video_id}}},
    ).execute()


# --------------------------------------------------------------------------- verify

def verify(youtube, state: State) -> dict:
    """Ask YouTube what actually happened to everything recorded as uploaded."""
    by_id = {v["video_id"]: k for k, v in state.data.items()
             if isinstance(v, dict) and v.get("video_id")}
    if not by_id:
        return {"healthy": [], "broken": []}

    info = {}
    ids = list(by_id)
    for i in range(0, len(ids), 50):
        res = youtube.videos().list(
            part="status,processingDetails,contentDetails,snippet",
            id=",".join(ids[i:i + 50]), maxResults=50).execute()
        for it in res.get("items", []):
            info[it["id"]] = it

    healthy, broken = [], []
    for vid, key in by_id.items():
        name = Path(key).name
        it = info.get(vid)
        if it is None:
            broken.append({"key": key, "name": name, "video_id": vid,
                           "why": "gone (deleted or never landed)"})
            continue
        st, pd = it.get("status", {}), it.get("processingDetails", {})
        us, ps = st.get("uploadStatus", "?"), pd.get("processingStatus", "?")
        if us in ("rejected", "failed", "deleted"):
            why = st.get("rejectionReason") or st.get("failureReason") or us
            broken.append({"key": key, "name": name, "video_id": vid, "why": f"{us}: {why}"})
        elif ps == "failed":
            broken.append({"key": key, "name": name, "video_id": vid,
                           "why": f"processing failed: {pd.get('processingFailureReason', '?')}"})
        else:
            healthy.append({"key": key, "name": name, "video_id": vid,
                            "why": "processing" if ps == "processing" else "ok",
                            "duration": it.get("contentDetails", {}).get("duration", "")})
    return {"healthy": healthy, "broken": broken}
