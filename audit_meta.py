#!/usr/bin/env python3
"""Compare titles/descriptions on my channel against the old channel.

Matches every old-channel video to my re-upload (duration + fuzzy title),
then reports which of mine still have a wrong/unedited title or description.

Run:   python audit_meta.py            (report to console + meta_report.csv)
       python audit_meta.py --min-score 0.5
"""
import argparse, csv, re, sys, unicodedata
from difflib import SequenceMatcher
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from ytuploader.core import get_service
from ytuploader.metadata import fetch_channel_videos

OLD_CHANNEL = "UC91KEfHFM4U3jHvhCd9VC5Q"
OLD_UPLOADS = "UU" + OLD_CHANNEL[2:]

def iso_secs(d):
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d or "")
    if not m: return -1
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h*3600 + mi*60 + s

def norm(t):
    t = unicodedata.normalize("NFC", t or "").lower()
    t = re.sub(r"[​﻿]", "", t)
    return re.sub(r"\s+", " ", t).strip()

def fetch_old(youtube):
    ids, page = [], None
    while True:
        res = youtube.playlistItems().list(part="contentDetails",
              playlistId=OLD_UPLOADS, maxResults=50, pageToken=page).execute()
        ids += [i["contentDetails"]["videoId"] for i in res.get("items", [])]
        page = res.get("nextPageToken")
        if not page: break
    out = []
    for i in range(0, len(ids), 50):
        res = youtube.videos().list(part="snippet,contentDetails",
              id=",".join(ids[i:i+50]), maxResults=50).execute()
        for it in res.get("items", []):
            sn = it["snippet"]
            out.append({"id": it["id"], "title": sn.get("title",""),
                        "description": sn.get("description",""),
                        "secs": iso_secs(it["contentDetails"]["duration"])})
    return out

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--min-score", type=float, default=0.45)
    args = p.parse_args()

    yt = get_service(HERE / "client_secret.json", HERE / "token.json")
    print("Reading old channel...")
    old = fetch_old(yt)
    print(f"  {len(old)} public video(s) on the old channel")
    print("Reading my channel...")
    mine = fetch_channel_videos(yt, log=lambda m: None)
    print(f"  {len(mine)} video(s) on my channel\n")

    # one-to-one assignment: duration within 2s, ranked by title similarity
    pairs = []
    for oi, o in enumerate(old):
        for mi, m in enumerate(mine):
            if abs(o["secs"] - iso_secs(m.duration)) > 2: continue
            r = SequenceMatcher(None, norm(o["title"]), norm(m.title)).ratio()
            if r >= args.min_score:
                pairs.append((r, oi, mi))
    pairs.sort(key=lambda x: -x[0])
    used_o, used_m, match = set(), set(), {}
    for r, oi, mi in pairs:
        if oi in used_o or mi in used_m: continue
        used_o.add(oi); used_m.add(mi); match[oi] = (mi, r)

    rows, n_title, n_desc, n_ok = [], 0, 0, 0
    print("=== NEEDS EDITING ===")
    for oi, o in enumerate(old):
        if oi not in match: continue
        mi, r = match[oi]
        m = mine[mi]
        bad_t = norm(m.title) != norm(o["title"])
        bad_d = norm(m.description) != norm(o["description"])
        if bad_t or bad_d:
            what = "+".join(x for x, b in (("title", bad_t), ("desc", bad_d)) if b)
            n_title += bad_t; n_desc += bad_d
            print(f"[{what:10}] {m.video_id} ({r:.2f})")
            print(f"   mine: {m.title}")
            if bad_t: print(f"   old : {o['title']}")
            rows.append({"fix": what, "my_id": m.video_id,
                         "my_url": f"https://youtu.be/{m.video_id}",
                         "score": f"{r:.2f}",
                         "my_title": m.title, "old_title": o["title"],
                         "my_description": m.description,
                         "old_description": o["description"]})
        else:
            n_ok += 1

    not_reuploaded = [o for oi, o in enumerate(old) if oi not in match]
    print(f"\n=== SUMMARY ===")
    print(f"matched: {len(match)}  ok: {n_ok}  bad title: {n_title}  bad description: {n_desc}")
    print(f"old videos with no match on my channel (not re-uploaded?): {len(not_reuploaded)}")
    for o in not_reuploaded:
        print(f"   {o['secs']:>5}s  https://youtu.be/{o['id']}  {o['title']}")

    out = HERE / "meta_report.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["fix","my_id","my_url","score",
                    "my_title","old_title","my_description","old_description"])
        w.writeheader(); w.writerows(rows)
    print(f"\nDetails written to {out.name} ({len(rows)} rows)")

if __name__ == "__main__":
    main()
