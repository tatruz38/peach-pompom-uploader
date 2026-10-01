#!/usr/bin/env python3
"""Copy the old channel's public playlists onto this channel.

Reads every public playlist on the old (compromised) channel, matches each
entry to the re-uploaded copy on YOUR channel by title, then recreates the
playlists and fills them in the same order.

    python playlists.py                # dry run: show the full plan
    python playlists.py --apply        # create playlists and add videos
    python playlists.py --apply --limit 30   # add at most N videos this run

Quota: creating a playlist costs 50 units, and EVERY video added costs 50.
A default project has 10,000/day, so roughly 190 adds is a full day. Progress
is saved in playlists_state.json after every call — rerun tomorrow and it
continues where it stopped, skipping everything already done.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from types import SimpleNamespace
from difflib import SequenceMatcher
from pathlib import Path

from googleapiclient.errors import HttpError

from ytuploader.core import get_service, stop_reason
from ytuploader.metadata import fetch_channel_videos

HERE = Path(__file__).resolve().parent
STATE = HERE / "playlists_state.json"
OLD_CHANNEL = "UC91KEfHFM4U3jHvhCd9VC5Q"


def log(m=""):
    print(m, flush=True)


def norm(t: str) -> str:
    t = unicodedata.normalize("NFC", t or "").lower()
    t = re.sub(r"[​-‏⁠﻿\xa0]", " ", t)
    t = re.sub(r"[\[\](){}|/@_,.:;!?'\"“”‘’#+-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"playlists": {}, "added": {}}


def save_state(s: dict):
    STATE.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")


def old_playlists(youtube) -> list[dict]:
    out, page = [], None
    while True:
        res = youtube.playlists().list(
            part="snippet,contentDetails,status", channelId=OLD_CHANNEL,
            maxResults=50, pageToken=page).execute()
        out += res.get("items", [])
        page = res.get("nextPageToken")
        if not page:
            return out


def playlist_items(youtube, pid: str) -> list[dict]:
    out, page = [], None
    while True:
        res = youtube.playlistItems().list(
            part="snippet", playlistId=pid,
            maxResults=50, pageToken=page).execute()
        out += res.get("items", [])
        page = res.get("nextPageToken")
        if not page:
            return out


def best_match(title: str, mine: list, min_score: float):
    n = norm(title)
    best, score = None, 0.0
    for v in mine:
        r = SequenceMatcher(None, n, norm(v.title)).ratio()
        if r > score:
            best, score = v, r
    return (best, score) if score >= min_score else (None, score)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="actually create and fill")
    p.add_argument("--limit", type=int, default=0,
                   help="max playlistItems.insert calls this run (0 = no cap)")
    p.add_argument("--min-score", type=float, default=0.75,
                   help="title match threshold (default 0.75)")
    p.add_argument("--privacy", choices=["public", "unlisted", "private"],
                   default="public", help="privacy for the new playlists")
    p.add_argument("--only", default="",
                   help="process only the old playlist whose ID or title contains this text")
    args = p.parse_args()

    youtube = get_service(HERE / "client_secret.json", HERE / "token.json")
    state = load_state()

    log("Reading old channel playlists...")
    pls = old_playlists(youtube)
    log(f"  {len(pls)} playlist(s) on the old channel")
    log("Reading your channel videos...")
    mine = fetch_channel_videos(youtube, log=lambda m: None)
    log(f"  {len(mine)} video(s) on your channel\n")

    plan, total_add, total_miss = [], 0, 0
    for pl in pls:
        pid = pl["id"]
        title = pl["snippet"]["title"]
        if args.only and args.only not in pid and args.only.lower() not in title.lower():
            continue
        items = playlist_items(youtube, pid)
        matched, missed = [], []
        # Split items: other channels' videos go in by their original id;
        # the old channel's own videos need matching to your re-uploads.
        seen_ids, own = set(), []
        entries = []                       # (kind, payload) in playlist order
        for it in items:
            sn = it["snippet"]
            t = sn["title"]
            if t in ("Deleted video", "Private video"):
                continue
            owner = sn.get("videoOwnerChannelId", "")
            if owner and owner != OLD_CHANNEL:
                orig = sn["resourceId"]["videoId"]
                if orig in seen_ids:
                    continue
                seen_ids.add(orig)
                entries.append(("ext", (t, SimpleNamespace(
                    video_id=orig, title=t + "  [original]"))))
            else:
                entries.append(("own", t))
                own.append(t)

        # One-to-one assignment, best scores first. Greedy per-item matching
        # breaks when a generically-titled upload is every item's best match:
        # all items collapse onto one video. Here each of your videos can be
        # claimed by only one playlist item.
        pairs = []
        for i, t in enumerate(own):
            n = norm(t)
            for v in mine:
                r = SequenceMatcher(None, n, norm(v.title)).ratio()
                if r >= args.min_score:
                    pairs.append((r, i, v))
        pairs.sort(key=lambda x: -x[0])
        assign: dict[int, object] = {}
        used: set[str] = set()
        for r, i, v in pairs:
            if i not in assign and v.video_id not in used:
                assign[i] = v
                used.add(v.video_id)

        oi = 0
        for kind, payload in entries:
            if kind == "ext":
                matched.append(payload)
                continue
            t = payload
            if oi in assign:
                matched.append((t, assign[oi]))
            else:
                _, sc = best_match(t, mine, 2.0)   # score only, for the report
                missed.append((t, sc))
            oi += 1
        already = set(state["added"].get(pid, []))
        todo = [(t, v) for t, v in matched if v.video_id not in already]
        plan.append((pid, title, pl, matched, todo, missed))
        total_add += len(todo)
        total_miss += len(missed)

        log(f"'{title}'  ({len(items)} items)")
        log(f"    matched {len(matched)}, already added {len(matched)-len(todo)}, "
            f"to add {len(todo)}, no match {len(missed)}")
        for t, sc in missed:
            log(f"      MISS ({sc:.2f}) {t[:70]}")

    new_lists = sum(1 for pid, *_ in plan if pid not in state["playlists"])
    cost = new_lists * 50 + total_add * 50
    log(f"\nPlan: {new_lists} playlist(s) to create, {total_add} video(s) to add, "
        f"{total_miss} unmatched. Quota needed: ~{cost} units.")
    if not args.apply:
        log("\nDry run — nothing changed. Add --apply to do it.")
        return

    inserts = 0
    try:
        for pid, title, pl, matched, todo, missed in plan:
            if pid not in state["playlists"]:
                res = youtube.playlists().insert(
                    part="snippet,status",
                    body={"snippet": {
                            "title": title,
                            "description": pl["snippet"].get("description", "")},
                          "status": {"privacyStatus": args.privacy}}).execute()
                state["playlists"][pid] = res["id"]
                save_state(state)
                log(f"created playlist '{title}' -> {res['id']}")
            new_pid = state["playlists"][pid]
            for t, v in todo:
                if args.limit and inserts >= args.limit:
                    log(f"\n--limit {args.limit} reached; rerun to continue.")
                    return
                youtube.playlistItems().insert(
                    part="snippet",
                    body={"snippet": {
                        "playlistId": new_pid,
                        "resourceId": {"kind": "youtube#video",
                                       "videoId": v.video_id}}}).execute()
                inserts += 1
                state["added"].setdefault(pid, []).append(v.video_id)
                save_state(state)
                log(f"  + [{title[:24]}] {v.title[:60]}")
    except HttpError as e:
        why = stop_reason(e)
        body = e.content.decode("utf-8", "ignore").lower()
        if not why and (e.resp.status == 429 or "rate_limit_exceeded" in body):
            why = ("YouTube's daily cap on creating playlists (separate from "
                   "the units quota)")
        if why:
            log(f"\nSTOP: {why}. Progress is saved — rerun tomorrow to continue.")
            sys.exit(0)
        raise

    log(f"\nDone. {inserts} video(s) added this run.")


if __name__ == "__main__":
    main()
