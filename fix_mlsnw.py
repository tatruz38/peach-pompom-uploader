#!/usr/bin/env python3
"""Fix titles/descriptions of the 4 re-uploaded mlsnw videos.

Dry run:   python fix_mlsnw.py
Apply:     python fix_mlsnw.py --apply
"""
import argparse, sys
from difflib import SequenceMatcher
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from ytuploader.core import get_service

CHANNEL = "UC6ZDDrKuUbf3gmlyYruT5ag"

# (key, duration_s, target_title, target_description)
HASH = "#mlsnw\n#MILKbkkMusicLabel\n#MILKbkk"
TARGETS = [
    ("ihateflowers", 242,
     "mlsnw (Millie Snow) - I hate flowers @ ลานเล่นล้อ มศว ประสานมิตร [4K]",
     "mlsnw (Millie Snow) - I hate flowers @ ลานเล่นล้อ มศว ประสานมิตร [4K]\n\n" + HASH),
    ("recall", 274,
     "mlsnw (Millie Snow) - วาดไว้ (recall) @ ลานเล่นล้อ มศว ประสานมิตร [4K]",
     "mlsnw (Millie Snow) - วาดไว้ (recall) @ ลานเล่นล้อ มศว ประสานมิตร [4K]\n\n" + HASH),
    ("star", 225,
     "mlsnw (Millie Snow) - You are the star @ ลานเล่นล้อ มศว ประสานมิตร [4K]",
     "mlsnw (Millie Snow) - You are the star @ ลานเล่นล้อ มศว ประสานมิตร [4K]\n\n" + HASH),
    ("old_ihateflowers", 242,
     "old mlsnw (Millie Snow) - I hate flowers @ ลานเล่นล้อ มศว ประสานมิตร [4K]",
     "new video - {NEW_LINK}"),
]

def iso_secs(d):
    import re
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d or "")
    if not m: return -1
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h*3600 + mi*60 + s

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    p.add_argument("--recent", type=int, default=25, help="how many latest uploads to scan")
    args = p.parse_args()

    yt = get_service(HERE / "client_secret.json", HERE / "token.json")

    ch = yt.channels().list(part="contentDetails", id=CHANNEL).execute()
    uploads = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    items = yt.playlistItems().list(part="contentDetails", playlistId=uploads,
                                    maxResults=args.recent).execute()["items"]
    ids = [it["contentDetails"]["videoId"] for it in items]
    vids = yt.videos().list(part="snippet,contentDetails,status",
                            id=",".join(ids)).execute()["items"]

    # candidates: duration within 2s of any target
    durs = {t[1] for t in TARGETS}
    cands = [v for v in vids
             if any(abs(iso_secs(v["contentDetails"]["duration"]) - d) <= 2 for d in durs)]
    print(f"Scanned {len(vids)} recent uploads, {len(cands)} duration-matched candidates:")
    for v in cands:
        print(f"  {v['id']}  {iso_secs(v['contentDetails']['duration'])}s  "
              f"[{v['status'].get('privacyStatus')}]  {v['snippet']['title']}")

    # one-to-one assignment by (duration match, title similarity)
    pairs = []
    for ti, (key, dur, title, _) in enumerate(TARGETS):
        for v in cands:
            if abs(iso_secs(v["contentDetails"]["duration"]) - dur) > 2:
                continue
            r = SequenceMatcher(None, title.lower(), v["snippet"]["title"].lower()).ratio()
            # the 'old' target should prefer a filename containing 'old'
            if key.startswith("old") == ("old" in v["snippet"]["title"].lower()):
                r += 0.5
            pairs.append((r, ti, v))
    pairs.sort(key=lambda x: -x[0])
    used_t, used_v, plan = set(), set(), {}
    for r, ti, v in pairs:
        if ti in used_t or v["id"] in used_v: continue
        used_t.add(ti); used_v.add(v["id"]); plan[ti] = (v, r)

    new_ihf_id = plan[0][0]["id"] if 0 in plan else None
    print("\nPlan:")
    for ti, (key, dur, title, desc) in enumerate(TARGETS):
        if ti not in plan:
            print(f"  MISS  {title}")
            continue
        v, r = plan[ti]
        d = desc.replace("{NEW_LINK}", f"https://youtu.be/{new_ihf_id}" if new_ihf_id else "")
        print(f"  {v['id']} ({r:.2f})  '{v['snippet']['title']}'")
        print(f"      -> title: {title}")
        print(f"      -> desc : {d.splitlines()[0]} ...")
        if args.apply:
            sn = v["snippet"]
            sn["title"], sn["description"] = title, d
            yt.videos().update(part="snippet",
                               body={"id": v["id"], "snippet": sn}).execute()
            print("      UPDATED")
    if not args.apply:
        print("\nDry run - nothing changed. Add --apply to update.")

if __name__ == "__main__":
    main()
