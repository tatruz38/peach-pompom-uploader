#!/usr/bin/env python3
"""Peach Pompom Uploader — command line interface.

    python cli.py video.mp4 --title "My video"
    python cli.py --csv videos.csv --limit 6
    python cli.py --verify --requeue-failed

For the graphical version, run:  python server.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from googleapiclient.errors import HttpError

from ytuploader import core
from ytuploader.core import Item, State

HERE = Path(__file__).resolve().parent


def log(m: str) -> None:
    print(m, flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description="Upload videos to YouTube.")
    p.add_argument("video", nargs="?", help="path to a video file")
    p.add_argument("--csv")
    p.add_argument("--video-dir", default="")
    p.add_argument("--title")
    p.add_argument("--description", default="")
    p.add_argument("--tags", default="")
    p.add_argument("--category", default="22")
    p.add_argument("--privacy", default="private", choices=["private", "unlisted", "public"])
    p.add_argument("--publish-at", default="")
    p.add_argument("--playlist", default="")
    p.add_argument("--thumbnail", default="")
    p.add_argument("--made-for-kids", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verify", action="store_true")
    p.add_argument("--requeue-failed", action="store_true")
    p.add_argument("--secrets", default=str(HERE / "client_secret.json"))
    p.add_argument("--token", default=str(HERE / "token.json"))
    p.add_argument("--state", default=str(HERE / "uploaded.json"))
    p.add_argument("--remux-dir", default="")
    p.add_argument("--keep-temp", action="store_true")
    args = p.parse_args()

    state = State(Path(args.state).expanduser())
    remux_dir = Path(args.remux_dir).expanduser() if args.remux_dir else None

    if args.verify:
        yt = core.get_service(Path(args.secrets).expanduser(), Path(args.token).expanduser())
        r = core.verify(yt, state)
        log(f"--- {len(r['healthy'])} healthy, {len(r['broken'])} broken ---")
        for b in r["broken"]:
            log(f"  BAD  {b['name']}\n       https://youtu.be/{b['video_id']} — {b['why']}")
        if r["broken"] and args.requeue_failed:
            n = state.drop([b["key"] for b in r["broken"]])
            log(f"\nRequeued {n} — they will upload again on the next run.")
            log("Delete the broken videos in YouTube Studio so you don't keep stubs.")
        elif r["broken"]:
            log("\nRerun with --requeue-failed to queue these for re-upload.")
        return

    if not args.video and not args.csv:
        p.error("give a video file, or --csv, or --verify")

    if args.csv:
        csv_path = Path(args.csv).expanduser().resolve()
        base = Path(args.video_dir).expanduser() if args.video_dir else None
        items = core.items_from_csv(csv_path, base)
    else:
        items = [Item(
            file=str(Path(args.video).expanduser()),
            title=args.title or "", description=args.description, tags=args.tags,
            category=args.category, privacy=args.privacy, publish_at=args.publish_at,
            playlist=args.playlist, thumbnail=args.thumbnail,
            made_for_kids="true" if args.made_for_kids else "",
        )]

    queue, missing, already = [], [], 0
    for it in items:
        if state.done(it.file):
            already += 1
        elif not Path(it.file).exists():
            missing.append(Path(it.file).name)
        else:
            queue.append(it)

    log(f"{len(items)} in list | {already} already uploaded | "
        f"{len(missing)} missing on disk | {len(queue)} ready")
    if missing:
        log("  missing: " + ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else ""))
    if args.limit:
        queue = queue[: args.limit]
        log(f"limited to {len(queue)} this run")

    if args.dry_run:
        for it in queue[:20]:
            log(f"  would upload: {Path(it.file).name} -> \"{it.title or Path(it.file).stem}\"")
        if len(queue) > 20:
            log(f"  ... and {len(queue) - 20} more")
        return
    if not queue:
        log("Nothing to do.")
        return

    yt = core.get_service(Path(args.secrets).expanduser(), Path(args.token).expanduser())
    ok, failed, stopped = 0, [], False

    for i, it in enumerate(queue, 1):
        log(f"\n[{i}/{len(queue)}] {Path(it.file).name}")
        last = [-5]

        def prog(pct, _last=last):
            if pct >= _last[0] + 5:
                log(f"  {pct}%")
                _last[0] = pct

        try:
            vid = core.upload_one(yt, it, remux_dir, args.keep_temp, prog,
                                  lambda m: log("  " + m))
            state.record(it.file, vid, it.title or Path(it.file).stem)
            ok += 1
        except HttpError as e:
            reason = core.stop_reason(e)
            if reason:
                log(f"\n  STOP: {reason} reached.")
                log("  Nothing is lost — rerun the same command later and it resumes here.")
                stopped = True
                break
            log(f"  FAILED: {e}")
            failed.append(Path(it.file).name)
        except Exception as e:
            log(f"  FAILED: {e}")
            failed.append(Path(it.file).name)

    log(f"\n--- {ok} uploaded, {len(failed)} failed, "
        f"{len(items) - len(state.data)} still to go ---")
    for n in failed:
        log(f"  FAILED {n}")
    sys.exit(1 if (failed or stopped) else 0)


if __name__ == "__main__":
    main()
