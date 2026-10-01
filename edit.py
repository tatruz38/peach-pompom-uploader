#!/usr/bin/env python3
"""Bulk-edit titles, descriptions and other metadata from a spreadsheet.

Three steps:

    python edit.py --export sheet.xlsx      # your channel → spreadsheet
    #   ...edit the yellow columns in Excel, leave blank to keep as-is...
    python edit.py --preview sheet.xlsx     # show exactly what would change
    python edit.py --apply sheet.xlsx       # do it

Rows are matched to videos by video_id, url, file (via uploaded.json), or
current_title — whichever the sheet provides, in that order.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ytuploader import core, metadata
from ytuploader.core import State

HERE = Path(__file__).resolve().parent


def log(m=""):
    print(m, flush=True)


def show(edits, limit=0, show_pending=False):
    changed = [e for e in edits if e.video and e.changes and not e.problem]
    pending = [e for e in edits if e.kind == "pending"]
    problems = [e for e in edits if e.kind == "problem"]
    nothing = len(edits) - len(changed) - len(pending) - len(problems)

    for e in (changed[:limit] if limit else changed):
        how = e.matched_by or "?"
        if e.matched_by == "fuzzy title":
            how = f"fuzzy title, {e.confidence:.0%} similar — CHECK THIS"
        log(f"\n  {e.label[:70]}")
        log(f"  https://youtu.be/{e.video.video_id}   [matched by {how}]")
        for f, (old, new) in e.changes.items():
            o = (old or "—").replace("\n", " ⏎ ")
            n = (new or "—").replace("\n", " ⏎ ")
            log(f"    {f}:")
            log(f"      - {o[:150]}")
            log(f"      + {n[:150]}")
    if limit and len(changed) > limit:
        log(f"\n  ... and {len(changed) - limit} more")

    if pending:
        log(f"\n  {len(pending)} row(s) not uploaded yet — nothing to edit until they are.")
        if show_pending:
            for e in pending:
                log(f"    row {e.row}: {e.label[:70]}")
        else:
            for e in pending[:3]:
                log(f"    e.g. row {e.row}: {e.label[:60]}")
            if len(pending) > 3:
                log(f"    ... and {len(pending) - 3} more (--show-pending to list them)")

    if problems:
        log("\n  Problems:")
        for e in problems:
            log(f"    row {e.row}: {e.problem}  ({e.label[:50]})")

    fuzzies = [e for e in changed if e.matched_by == "fuzzy title"]
    if fuzzies:
        log(f"\n  {len(fuzzies)} row(s) matched approximately — read those diffs "
            f"carefully before applying.")

    log(f"\n--- {len(changed)} to change · {nothing} already correct · "
        f"{len(pending)} not uploaded yet · {len(problems)} problems ---")
    return changed, problems


def main():
    p = argparse.ArgumentParser(description="Bulk-edit YouTube video metadata from a spreadsheet.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--export", metavar="FILE", help="write your channel to a new .xlsx or .csv")
    g.add_argument("--preview", metavar="FILE", help="show what a filled-in sheet would change")
    g.add_argument("--apply", metavar="FILE", help="apply the changes in a filled-in sheet")
    g.add_argument("--enrich", metavar="FILE",
                   help="rebuild titles and descriptions from the original uploads "
                        "referenced in the sheet, into a new file (--out)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--out", metavar="FILE", help="output file for --enrich")
    p.add_argument("--only", choices=["both", "titles", "descriptions"], default="both",
                   help="with --enrich: fill only titles, only descriptions, or both. "
                        "Blank cells are left untouched when applied.")
    p.add_argument("--no-date", action="store_true",
                   help="with --enrich: don't put a date at the front of titles")
    p.add_argument("--date-from-publish", action="store_true",
                   help="with --enrich: when the filename has no date, fall back to the "
                        "original video's publish date (often the upload day, not the "
                        "event day — check before trusting it)")
    p.add_argument("--limit", type=int, default=0, help="only show/apply the first N changes")
    p.add_argument("--show-pending", action="store_true",
                   help="list every row whose video has not been uploaded yet")
    p.add_argument("--match-by", choices=["auto", "title", "description"], default="auto",
                   help="auto: video_id/url/file/current_title. title: also treat the "
                        "'title' column as the video's existing title. description: "
                        "also fall back to matching on description text.")
    p.add_argument("--fuzzy", nargs="?", const=0.90, type=float, default=0.0,
                   metavar="RATIO",
                   help="allow approximate title matches (default ratio 0.90). Each "
                        "fuzzy match is shown with its score in preview.")
    p.add_argument("--secrets", default=str(HERE / "client_secret.json"))
    p.add_argument("--token", default=str(HERE / "token.json"))
    p.add_argument("--state", default=str(HERE / "uploaded.json"))
    args = p.parse_args()

    youtube = core.get_service(Path(args.secrets).expanduser(), Path(args.token).expanduser())
    state = State(Path(args.state).expanduser())

    if args.enrich:
        src = Path(args.enrich).expanduser()
        if not src.exists():
            sys.exit(f"not found: {src}")
        out = Path(args.out).expanduser() if args.out else src.with_name(src.stem + "-enriched.xlsx")
        log(f"reading {src.name}")
        s = metadata.enrich_sheet(youtube, src, out, keep_date=not args.no_date,
                                  use_publish_date=args.date_from_publish,
                                  only=args.only, log=log)
        log(f"\nwrote {out}")
        log(f"  {s['matched']} row(s) enriched from the original uploads")
        if s["not_found"]:
            log(f"  {s['not_found']} reference video(s) no longer available")
        if s["no_reference"]:
            log(f"  {s['no_reference']} row(s) had no reference URL")
        log(f"  dates: {s['dated_from_name']} from filename"
            + (f", {s['dated_from_publish']} from publish date" if s['dated_from_publish'] else "")
            + (f", {s['undated']} with no date" if s['undated'] else ""))
        log("\nOpen it, check the title column, then:")
        log(f"  python edit.py --preview {out.name} --match-by title --fuzzy")
        return

    if args.export:
        out = Path(args.export).expanduser()
        videos = metadata.fetch_channel_videos(youtube, log)
        metadata.export_sheet(out, videos, state.data)
        log(f"wrote {len(videos)} row(s) to {out}")
        log("\nEdit the yellow columns (title, description, tags, category, privacy,")
        log("publish_at). Leave a cell blank to leave that field untouched.")
        log(f"Then:  python edit.py --preview {out.name}")
        return

    path = Path(args.preview or args.apply).expanduser()
    if not path.exists():
        sys.exit(f"not found: {path}")

    keep, dropped = metadata.sheet_columns(path)
    rows = metadata.read_sheet(path)
    log(f"{len(rows)} row(s) in {path.name}")
    log(f"  using columns: {', '.join(keep) or 'none recognised'}")
    if dropped:
        log(f"  ignoring: {', '.join(dropped)}")
        log("  (columns describing the original/reference video are never used to "
            "match — they point at a different video than the one being edited)")
    if not any(c in keep for c in metadata.MATCH_COLS):
        sys.exit("No column identifies which video to edit. Add one of: "
                 + ", ".join(metadata.MATCH_COLS))
    videos = metadata.fetch_channel_videos(youtube, log)
    edits = metadata.resolve(rows, videos, state.data,
                             match_by=args.match_by, fuzzy=args.fuzzy)
    changed, problems = show(edits, limit=args.limit if args.preview else 0,
                             show_pending=args.show_pending)

    if args.preview:
        if changed:
            log(f"\nNothing has been sent to YouTube. To apply:  python edit.py --apply {path.name}")
        return

    if not changed:
        log("Nothing to do.")
        return
    if args.limit:
        changed = changed[: args.limit]
        log(f"limited to {len(changed)} this run")

    if not args.yes:
        log(f"\nAbout to update {len(changed)} video(s) on your channel.")
        try:
            if input("Type 'yes' to continue: ").strip().lower() != "yes":
                log("cancelled")
                return
        except (EOFError, KeyboardInterrupt):
            log("\ncancelled")
            return

    result = metadata.apply_all(youtube, changed, log)
    log(f"\n--- {result['updated']} updated, {len(result['failed'])} failed ---")
    for label, err in result["failed"]:
        log(f"  FAILED {label[:60]}: {err}")
    sys.exit(1 if result["failed"] else 0)


if __name__ == "__main__":
    main()
