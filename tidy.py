#!/usr/bin/env python3
"""Normalise titles and restore descriptions on your channel — no spreadsheet.

    python tidy.py                    # dry run: show every change, send nothing
    python tidy.py --apply            # make the changes
    python tidy.py --apply --limit 5  # do a few first

Titles are cleaned in place. Descriptions are recovered from the old channel by
matching titles, so a video keeps the text it originally had — hashtags, camera
notes and all.

    --old-channel   ID of the channel to recover descriptions from
    --no-descriptions / --no-titles   do only one of the two jobs
    --min-score     how close a title match must be to be trusted (default 0.80)
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

from googleapiclient.errors import HttpError

from ytuploader import core, metadata
from ytuploader.metadata import Video, normalize_title, extract_date

HERE = Path(__file__).resolve().parent
OLD_CHANNEL = "UC91KEfHFM4U3jHvhCd9VC5Q"      # the original Peach Pompom channel


def log(m=""):
    print(m, flush=True)


def fetch_public_channel(youtube, channel_id: str) -> list[Video]:
    """Every video on any public channel, via its uploads playlist."""
    uploads = "UU" + channel_id[2:]
    ids, page = [], None
    while True:
        try:
            res = youtube.playlistItems().list(
                part="contentDetails", playlistId=uploads,
                maxResults=50, pageToken=page).execute()
        except HttpError as e:
            raise RuntimeError(f"can't read channel {channel_id}: {e}") from e
        ids += [i["contentDetails"]["videoId"] for i in res.get("items", [])]
        page = res.get("nextPageToken")
        if not page:
            break
    return list(metadata.fetch_videos_by_id(youtube, ids, log=lambda m: None).values())


def best_match(title: str, pool: dict[str, Video], min_score: float):
    """Closest title in pool, with its similarity score."""
    key = core.norm_name(title)
    if key in pool:
        return pool[key], 1.0
    best, score = None, 0.0
    for cand_key, v in pool.items():
        r = difflib.SequenceMatcher(None, key, cand_key).ratio()
        if r > score:
            best, score = v, r
    return (best, score) if best and score >= min_score else (None, score)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="actually send the changes")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--old-channel", default=OLD_CHANNEL)
    p.add_argument("--no-titles", action="store_true")
    p.add_argument("--no-descriptions", action="store_true")
    p.add_argument("--min-score", type=float, default=0.80)
    p.add_argument("--no-date", action="store_true",
                   help="don't keep a date at the front of titles")
    p.add_argument("--secrets", default=str(HERE / "client_secret.json"))
    p.add_argument("--token", default=str(HERE / "token.json"))
    args = p.parse_args()

    if args.no_titles and args.no_descriptions:
        sys.exit("nothing to do — you disabled both jobs")

    youtube = core.get_service(Path(args.secrets).expanduser(), Path(args.token).expanduser())

    log("reading your channel…")
    mine = metadata.fetch_channel_videos(youtube, log)

    old_pool: dict[str, Video] = {}
    if not args.no_descriptions:
        log(f"reading {args.old_channel} for original descriptions…")
        try:
            old = fetch_public_channel(youtube, args.old_channel)
            for v in old:
                old_pool.setdefault(core.norm_name(v.title), v)
            log(f"  {len(old)} video(s) available to match against")
        except RuntimeError as e:
            log(f"  {e}")
            log("  continuing without descriptions")

    planned, weak = [], []
    for v in mine:
        changes = {}

        if not args.no_titles:
            new_title = normalize_title(v.title, extract_date(v.title), not args.no_date)
            if new_title and new_title != v.title:
                changes["title"] = (v.title, new_title)

        if old_pool and not v.description.strip():
            src, score = best_match(v.title, old_pool, args.min_score)
            if src and src.description.strip():
                changes["description"] = (v.description, src.description)
                if score < 0.95:
                    weak.append((v, score))

        if changes:
            planned.append((v, changes))

    for v, changes in planned:
        log(f"\n  {v.title[:70]}")
        log(f"  https://youtu.be/{v.video_id}")
        for field, (old_val, new_val) in changes.items():
            o = (old_val or "—").replace("\n", " ⏎ ")
            n = (new_val or "—").replace("\n", " ⏎ ")
            log(f"    {field}:")
            log(f"      - {o[:140]}")
            log(f"      + {n[:140]}")

    if weak:
        log("\n  Descriptions matched approximately — check these:")
        for v, score in weak:
            log(f"    {score:.0%}  {v.title[:60]}")

    log(f"\n--- {len(planned)} video(s) would change, {len(mine) - len(planned)} already fine ---")

    if not args.apply:
        log("\nDry run — nothing was sent. Add --apply to make these changes.")
        return
    if not planned:
        return

    todo = planned[: args.limit] if args.limit else planned
    log(f"\nApplying to {len(todo)} video(s)…")
    ok, failed = 0, []
    for v, changes in todo:
        # videos.update replaces the parts you send, so rebuild the whole snippet.
        snippet = {
            "title": changes.get("title", (None, v.title))[1],
            "description": changes.get("description", (None, v.description))[1],
            "tags": v.tags,
            "categoryId": v.category or "22",
        }
        try:
            youtube.videos().update(part="snippet",
                                    body={"id": v.video_id, "snippet": snippet}).execute()
            ok += 1
            log(f"  updated {v.video_id}  {', '.join(changes)}")
        except HttpError as e:
            failed.append((v.video_id, str(e)[:200]))
            log(f"  FAILED {v.video_id}: {e}")

    log(f"\n--- {ok} updated, {len(failed)} failed ---")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
