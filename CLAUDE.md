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
- `cli.py` — argparse front end.
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
