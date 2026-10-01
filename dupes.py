#!/usr/bin/env python3
"""Find likely duplicate videos on the channel. Report only — deletes nothing.

    python dupes.py              # print duplicate groups
    python dupes.py --strict     # only exact title+duration matches
    python dupes.py --csv        # also write dupes_report.csv

Deleting (two steps, on purpose):

    python dupes.py --strict --csv          # 1. write the report
    # open dupes_report.csv, check every row; the 'suggestion' column decides:
    #   dupe  -> will be deleted     keep? (or anything else) -> kept
    python dupes.py --delete dupes_report.csv   # 2. delete the 'dupe' rows

Deletion is PERMANENT — views, comments and the video id are gone for good.
The delete step only ever reads the CSV you reviewed, refuses to empty out a
whole group, and makes you type DELETE before it touches anything.

Two videos count as likely duplicates when their durations match exactly
(YouTube-reported, to the second) and their titles are essentially the same
after normalisation — punctuation, case, extra spaces and a trailing
"1"/"(1)"/"copy" ignored. That catches both re-uploads of one file and the
Studio-vs-CLI double uploads.
"""

from __future__ import annotations

import argparse
import csv
import re
import unicodedata
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path

from ytuploader.core import get_service
from ytuploader.metadata import fetch_channel_videos

HERE = Path(__file__).resolve().parent


def norm_title(t: str) -> str:
    t = unicodedata.normalize("NFC", t).lower()
    t = re.sub(r"[​-‏⁠﻿\xa0]", " ", t)
    t = re.sub(r"[\[\](){}|/@_,.:;!?'\"“”‘’#+-]", " ", t)
    # strip common re-upload suffixes: "... 1", "...(1)", "... copy"
    t = re.sub(r"\s+(copy|final|v\d)$", "", t.strip())
    t = re.sub(r"\s+\d$", "", t.strip())
    return re.sub(r"\s+", " ", t).strip()


def dur_seconds(iso: str) -> int:
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return -1
    h, mi, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + s


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--strict", action="store_true",
                   help="only exact normalised-title matches (no fuzzy)")
    p.add_argument("--ratio", type=float, default=0.90,
                   help="fuzzy title similarity threshold (default 0.90)")
    p.add_argument("--csv", action="store_true", help="write dupes_report.csv")
    p.add_argument("--delete", metavar="REPORT_CSV",
                   help="delete rows marked 'dupe' in a reviewed report CSV")
    args = p.parse_args()

    if args.delete:
        do_delete(Path(args.delete))
        return

    youtube = get_service(HERE / "client_secret.json", HERE / "token.json")
    videos = fetch_channel_videos(youtube)

    # Bucket by exact duration first — a real duplicate always shares it.
    by_dur: dict[int, list] = {}
    for v in videos:
        s = dur_seconds(v.duration)
        if s > 0:
            by_dur.setdefault(s, []).append(v)

    # Union-find inside each duration bucket.
    groups: list[list] = []
    for sec, vids in sorted(by_dur.items()):
        if len(vids) < 2:
            continue
        parent = {v.video_id: v.video_id for v in vids}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for a, b in combinations(vids, 2):
            na, nb = norm_title(a.title), norm_title(b.title)
            same = (na == nb) if args.strict else (
                na == nb or SequenceMatcher(None, na, nb).ratio() >= args.ratio)
            if same:
                parent[find(a.video_id)] = find(b.video_id)

        clusters: dict[str, list] = {}
        for v in vids:
            clusters.setdefault(find(v.video_id), []).append(v)
        groups += [g for g in clusters.values() if len(g) > 1]

    if not groups:
        print("\nNo likely duplicates found.")
        return

    groups.sort(key=lambda g: -len(g))
    total_extra = sum(len(g) - 1 for g in groups)
    print(f"\n{len(groups)} duplicate group(s); "
          f"{total_extra} video(s) could be removed.\n")

    rows = []
    for i, g in enumerate(groups, 1):
        g.sort(key=lambda v: v.published_at)     # oldest first
        m, s = divmod(dur_seconds(g[0].duration), 60)
        print(f"[{i}] duration {m}:{s:02d}")
        for j, v in enumerate(g):
            mark = "KEEP? " if j == 0 else "dupe  "
            print(f"    {mark} https://youtu.be/{v.video_id}  "
                  f"[{v.privacy:8s}] {v.title[:70]}")
            rows.append([i, "keep?" if j == 0 else "dupe", v.video_id,
                         f"https://youtu.be/{v.video_id}", v.privacy,
                         g[0].duration, v.published_at, v.title])
        print()

    print("KEEP? = oldest of the group (usually the one with views/description).")
    print("Nothing was deleted. Review in Studio before removing anything.")

    if args.csv:
        out = HERE / "dupes_report.csv"
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["group", "suggestion", "video_id", "url", "privacy",
                        "duration", "published_at", "title"])
            w.writerows(rows)
        print(f"\nWrote {out.name}")


def do_delete(report: Path):
    if not report.exists():
        raise SystemExit(f"{report} not found — run with --csv first")

    with report.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows or "suggestion" not in rows[0] or "video_id" not in rows[0]:
        raise SystemExit("that file doesn't look like a dupes_report.csv")

    targets = [r for r in rows if r["suggestion"].strip().lower() == "dupe"]
    if not targets:
        print("No rows marked 'dupe' — nothing to delete.")
        return

    # Never let a whole group disappear: every group must keep >= 1 video.
    kept_groups = {r["group"] for r in rows
                   if r["suggestion"].strip().lower() != "dupe"}
    orphaned = sorted({r["group"] for r in targets} - kept_groups)
    if orphaned:
        raise SystemExit(
            f"group(s) {', '.join(orphaned)} have EVERY row marked 'dupe' — "
            "that would delete all copies. Mark one row to keep and rerun.")

    print(f"\nThese {len(targets)} video(s) will be PERMANENTLY deleted:\n")
    for r in targets:
        print(f"  {r['url']}  [{r['privacy']}] {r['title'][:70]}")
    print("\nViews, comments and the video ids are unrecoverable."
          f"\nQuota cost: ~{50 * len(targets)} units.")
    answer = input("\nType DELETE (all caps) to proceed: ").strip()
    if answer != "DELETE":
        print("Not confirmed — nothing deleted.")
        return

    youtube = get_service(HERE / "client_secret.json", HERE / "token.json")
    done, failed = 0, 0
    for r in targets:
        vid = r["video_id"].strip()
        try:
            youtube.videos().delete(id=vid).execute()
            done += 1
            print(f"  deleted {vid}  {r['title'][:60]}")
        except Exception as e:
            failed += 1
            print(f"  FAILED  {vid}  {str(e).splitlines()[0][:90]}")
    print(f"\n--- {done} deleted, {failed} failed ---")


if __name__ == "__main__":
    main()
