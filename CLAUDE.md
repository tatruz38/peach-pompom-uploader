# Peach Pompom Uploader — notes for Claude Code

## What this is

A personal tool for restoring a YouTube channel's video archive after the
original channel was lost. It uploads local video files to the owner's own
channel with titles and descriptions recovered from the originals. Single user,
not distributed, no server component.

## Architecture

- `ytuploader/core.py` — everything that talks to YouTube or ffmpeg. OAuth,
  remux, resumable upload, `State`, `verify()`. **Both front ends call this; put
  new logic here, not in the UIs.**
- `cli.py` — argparse front end for uploading.
- `ytuploader/metadata.py` + `edit.py` — bulk metadata editing from a
  spreadsheet. Export → edit in Excel → preview → apply.
- `server.py` — stdlib `ThreadingHTTPServer`. No web framework, deliberately:
  the only dependencies are the Google API client libraries. Progress reaches
  the browser over Server-Sent Events at `/api/events`.
- `web/index.html` — the whole UI, one file, no build step, no framework.

Keep it dependency-light. If a change needs a new package, it probably belongs
somewhere else.

## Things that are easy to get wrong

- **YouTube rejects `.mkv`.** `NEEDS_REMUX` lists the containers that must be
  re-wrapped first. Remux is `-c copy` (no re-encode) with an AAC fallback when
  stream copy fails.
- **Two different daily limits.** API quota (`quotaExceeded`) and the per-channel
  upload cap (`uploadLimitExceeded`) are unrelated. `core.stop_reason()` detects
  both; on either, stop the whole run — continuing just burns quota on errors.
- **Upload success is not acceptance.** `videos.insert` returns as soon as the
  bytes land. YouTube can still reject the video during processing (too long for
  an unverified channel, duplicate, copyright). `core.verify()` is the only way
  to find out. Never treat a returned video ID as proof it went live.
- **Filenames.** Source lists come from spreadsheets and often carry NFD/NFC
  differences and zero-width characters that macOS filenames don't have. Use
  `core.norm_name()` when matching names to files.
- **`State` keys are absolute source paths**, not the remuxed temp file.
- **`videos.update` replaces the parts you send.** Omit `tags` from a snippet
  update and YouTube deletes them; omit `categoryId` and the call fails.
  `metadata.apply_edit()` always rebuilds the full snippet from the video's
  current values and overlays only the changed fields. Never construct an
  update body from the spreadsheet row alone.
- **Never guess a match.** Rows resolve by video_id → url → file → title. A
  title matching two videos is a reported problem, not a coin flip.

## Conventions

- Comments explain *why*, not what. If a line needs a comment to say what it
  does, rewrite the line.
- Errors that the user can act on should say what to do next.
- Never log or commit `client_secret.json`, `token.json`, or channel-identifying
  data beyond what's already public.

## Testing

There is no test suite yet. To exercise the pipeline without touching YouTube:

```bash
ffmpeg -f lavfi -i testsrc=size=320x240:rate=10:duration=2 \
       -f lavfi -i sine=duration=2 -c:v libx264 -c:a aac /tmp/vids/sample.mkv
python cli.py --csv videos.example.csv --dry-run
python -c "from ytuploader import core; from pathlib import Path; \
  print(core.remux_to_mp4(Path('/tmp/vids/sample.mkv'), Path('/tmp/out')))"
```

## Drafts (learned the hard way, 2026-08-20)

**Studio's bulk "Edit → Visibility" silently does nothing to drafts.** With
"Select all matching" over 103 drafts it reported *"103 of 103 videos
successfully updated"* and not one changed — all still Draft after a reload. It
works correctly on already-uploaded videos. Never trust that toast for drafts;
always reload and re-check the filter count.

**Always sort the draft list oldest-first.** Newest-first puts videos that are
still uploading or still running copyright checks at the top, and Studio keeps
the Visibility step disabled until checks finish. A loop that always takes the
first row will sit on such a video forever — that was the original
`finish_drafts.py` "stuck" bug. The URL now carries
`sort={"columnType":"date","sortOrder":"ASCENDING"}`, and the script steps past
a not-ready draft instead of retrying it.

**Every Studio drag-and-drop creates one of these.** A draft arrives with a
mangled title (filename, with a double space where the " - " was) and no
description. `python cli.py --csv ...` arrives Private, titled and described, and
never creates a draft. Prefer the CLI for bulk work; the browser path is a
last resort at roughly two browser actions per video.
