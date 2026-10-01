#!/usr/bin/env python3
"""Finish YouTube drafts and set their visibility — by driving YouTube Studio.

The YouTube Data API cannot see or edit drafts: a draft has never been committed
with its required fields, so it doesn't exist as a video yet. Studio's own bulk
"Edit > Visibility" does not work on drafts either — it reports "N of N videos
successfully updated" and changes nothing. The only thing that actually finishes
a draft is opening it in Studio. This script does that, one at a time.

    pip install playwright
    playwright install chromium

    python finish_drafts.py --login            # once: sign in, then close the window
    python finish_drafts.py                    # dry run: count drafts, change nothing
    python finish_drafts.py --apply            # finish them, set each Private
    python finish_drafts.py --apply --visibility public

It uses its own browser profile in .studio-profile/, so it never touches your
normal Chrome and you stay signed in between runs.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright
except ImportError:
    sys.exit("Playwright is missing. Run:\n"
             "    pip install playwright\n"
             "    playwright install chromium")

HERE = Path(__file__).resolve().parent
PROFILE = HERE / ".studio-profile"
CHANNEL = "UC6ZDDrKuUbf3gmlyYruT5ag"

# Oldest first. This matters: sorted newest-first, Studio puts videos that are
# still uploading or still running copyright checks at the top, and it keeps the
# Visibility step disabled until checks finish. A loop that always takes the
# first row will sit on such a video forever.
DRAFT_URL = ("https://studio.youtube.com/channel/{ch}/videos/upload"
             '?filter=[{{"name":"VISIBILITY","value":["DRAFT"]}}]'
             '&sort={{"columnType":"date","sortOrder":"ASCENDING"}}')

SEL_EDIT_DRAFT = 'ytcp-button:has-text("Edit draft"), button:has-text("Edit draft")'
SEL_DIALOG = "ytcp-video-metadata-editor:visible"
SEL_STEP_VISIBILITY = "#step-badge-3"
SEL_SAVE = "#done-button"
SEL_CLOSE = "ytcp-button#close-button, tp-yt-iron-icon#close-icon"

# The upload dialog is taller than most laptop screens. At 100% the Save button
# sits below the fold and clicks silently miss it, so shrink the page while the
# dialog is open.
ZOOM_OUT = "document.documentElement.style.zoom = '0.5'"
ZOOM_RESET = "document.documentElement.style.zoom = '1'"

RADIO = {
    "private": 'tp-yt-paper-radio-button[name="PRIVATE"]',
    "unlisted": 'tp-yt-paper-radio-button[name="UNLISTED"]',
    "public": 'tp-yt-paper-radio-button[name="PUBLIC"]',
}


def log(m=""):
    print(m, flush=True)


def open_browser(pw, headed: bool):
    return pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE),
        headless=not headed,
        viewport={"width": 1600, "height": 1200},
        args=["--disable-blink-features=AutomationControlled",
              "--window-size=1600,1200"],
    )


def js(page, script):
    try:
        page.evaluate(script)
    except Exception:
        pass


def click_safely(locator):
    """Click a control even if it is scrolled off the bottom of the window.

    The upload dialog is taller than most screens, so Save can sit below the
    fold no matter how the window is sized. Try the real click first (it is
    closest to a human), then fall back to dispatching the click on the element
    itself, which ignores viewport geometry entirely.
    """
    try:
        locator.scroll_into_view_if_needed(timeout=5_000)
    except Exception:
        pass
    try:
        locator.click(timeout=8_000)
        return
    except Exception:
        pass
    locator.evaluate("el => el.click()")


def count_drafts(page, settle: float = 2.5) -> int:
    page.wait_for_timeout(int(settle * 1000))
    try:
        return page.locator(SEL_EDIT_DRAFT).count()
    except Exception:
        return 0


def draft_ids(page, settle: float) -> list[str]:
    """Video ids of the drafts currently listed, in page order.

    Clicking "Edit draft" just sends Studio to the same list URL with
    &d=ud&udvid=<id>, so once we know the ids we can open each dialog by URL
    and never depend on a button click landing.
    """
    page.wait_for_timeout(int(settle * 1000))
    # Draft titles are not links (a draft has no watch page yet), so read the id
    # off the row thumbnail instead: Studio serves those from
    # https://i.ytimg.com/vi/<VIDEO_ID>/... which carries the id we need.
    blobs = page.eval_on_selector_all(
        "ytcp-video-row",
        """els => els.map(e => {
             const img = e.querySelector('img');
             const a = e.querySelector('a[href*=\"/video/\"]');
             return (img && (img.src || img.getAttribute('src')) || '')
                    + ' ' + (a && a.getAttribute('href') || '');
           })""")
    ids, seen = [], set()
    for b in blobs:
        m = (re.search(r"/vi/([A-Za-z0-9_-]{11})/", b or "")
             or re.search(r"/video/([A-Za-z0-9_-]{11})", b or ""))
        vid = m.group(1) if m else ""
        # One row -> one entry, so a row we can't identify still holds its place.
        if vid and vid not in seen:
            seen.add(vid)
            ids.append(vid)
    return ids


class NotReady(Exception):
    """This draft is still uploading or still in checks — try it later."""


def finish_one(page, url: str, vid: str, visibility: str, patience: int) -> str:
    """Open the draft dialog for `vid` by URL, set visibility, save."""
    dialog = page.locator(SEL_DIALOG)

    if vid:
        page.goto(f"{url}&d=ud&udvid={vid}")
        try:
            dialog.wait_for(state="visible", timeout=15_000)
        except PWTimeout:
            vid = ""  # fall through to the button path below
    if not vid:
        # Click the first row's button. dispatch_event fires the event on the
        # element regardless of where it sits on screen.
        btn = page.locator(SEL_EDIT_DRAFT).nth(0)
        for attempt in range(4):
            try:
                if attempt == 0:
                    btn.click(timeout=8_000)
                else:
                    btn.dispatch_event("click")
            except Exception:
                pass
            try:
                dialog.wait_for(state="visible", timeout=12_000)
                break
            except PWTimeout:
                page.wait_for_timeout(1_500)
        else:
            raise NotReady("(dialog never opened via button)")

    js(page, ZOOM_OUT)
    page.wait_for_timeout(400)

    title = "(untitled)"
    for sel in ("#dialog-title", "#textbox"):
        try:
            t = dialog.locator(sel).first.inner_text(timeout=3_000).strip()
            if t:
                title = t.splitlines()[0]
                break
        except Exception:
            continue

    radio = page.locator(RADIO[visibility])

    # Jump straight to Visibility — Details needs nothing from us. The step stays
    # disabled while the upload finishes and while copyright checks run, so poll
    # briefly; if it never opens, this one isn't ready and we move on rather than
    # blocking the whole queue on it.
    step = page.locator(SEL_STEP_VISIBILITY)
    step.wait_for(state="visible", timeout=30_000)
    for _ in range(patience):
        try:
            step.click(timeout=2_000)
            radio.wait_for(state="visible", timeout=3_000)
            break
        except PWTimeout:
            page.wait_for_timeout(2_000)
    else:
        js(page, ZOOM_RESET)
        try:
            page.locator(SEL_CLOSE).first.click(timeout=3_000)
        except Exception:
            page.keyboard.press("Escape")
        raise NotReady(title)

    # Studio already has Private selected when a draft opens, so for the default
    # case the whole job is: click Visibility, click Save. Only touch the radio
    # when it isn't already the one we want -- an unnecessary click here is the
    # one step that can fail while everything else is fine.
    already = False
    try:
        already = radio.get_attribute("aria-checked", timeout=3_000) == "true"
    except Exception:
        pass
    if not already:
        try:
            click_safely(radio)
            page.wait_for_timeout(600)
        except Exception:
            if visibility != "private":
                raise

    save = page.locator(SEL_SAVE)
    save.wait_for(state="attached", timeout=15_000)
    click_safely(save)

    dialog.wait_for(state="hidden", timeout=90_000)
    js(page, ZOOM_RESET)
    page.wait_for_timeout(1_500)
    return title


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="actually finish the drafts")
    p.add_argument("--login", action="store_true",
                   help="open a browser so you can sign in, then exit")
    p.add_argument("--channel", default=CHANNEL)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--visibility", choices=sorted(RADIO), default="private",
                   help="what to set each finished draft to (default: private)")
    p.add_argument("--patience", type=int, default=10,
                   help="~2s polls to wait for the Visibility step (default 10 = 20s)")
    p.add_argument("--settle", type=float, default=2.5,
                   help="seconds to let the draft list render (default 2.5)")
    p.add_argument("--probe2", action="store_true",
                   help="open one draft dialog and report its real structure")
    p.add_argument("--probe", action="store_true",
                   help="report which selectors match on the draft list, then exit")
    p.add_argument("--hold", action="store_true",
                   help="leave the browser open at the end so you can look")
    p.add_argument("--headless", action="store_true",
                   help="hide the browser (watch it the first few times instead)")
    args = p.parse_args()

    url = DRAFT_URL.format(ch=args.channel)

    with sync_playwright() as pw:
        ctx = open_browser(pw, headed=not args.headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        if args.login:
            page.goto("https://studio.youtube.com/")
            log("Sign in to YouTube Studio in the window that opened.")
            log("When your channel dashboard is showing, press Enter here.")
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                pass
            ctx.close()
            log("Saved. Now run:  python finish_drafts.py")
            return

        log(f"opening: {url}")
        page.goto(url)

        if args.probe:
            page.wait_for_timeout(int(args.settle * 1000))
            log(f"\n  page url:   {page.url}")
            log(f"  page title: {page.title()}\n")
            candidates = {
                "ytcp-button:has-text(Edit draft)": 'ytcp-button:has-text("Edit draft")',
                "button:has-text(Edit draft)": 'button:has-text("Edit draft")',
                "text=Edit draft": "text=Edit draft",
                "#edit-draft-button": "#edit-draft-button",
                "ytcp-video-row": "ytcp-video-row",
                "#video-title": "#video-title",
                "a[href*=/edit]": 'a[href*="/edit"]',
                "tp-yt-paper-checkbox": "tp-yt-paper-checkbox",
            }
            for label, sel in candidates.items():
                try:
                    c = page.locator(sel).count()
                except Exception as e:
                    c = f"error: {str(e).splitlines()[0][:60]}"
                log(f"  {c:>6}  {label}")
            ids = draft_ids(page, 0.1)
            log(f"\n  video ids extracted: {len(ids)}")
            if ids:
                log(f"  first three: {ids[:3]}")
            else:
                sample = page.eval_on_selector_all(
                    "ytcp-video-row",
                    "els => els.slice(0,1).map(e => e.outerHTML.slice(0,700))")
                log("  no ids found. first row markup:")
                log("  " + (sample[0] if sample else "(no rows)"))
            log("")
            if args.hold:
                log("  Browser left open. Press Enter here to close it.")
                try:
                    input()
                except (EOFError, KeyboardInterrupt):
                    pass
            ctx.close()
            return

        if args.probe2:
            ids = draft_ids(page, max(args.settle, 4.0))
            log(f"  ids: {len(ids)}")
            if not ids:
                log("  no ids - cannot probe the dialog"); ctx.close(); return
            log(f"  opening dialog for {ids[0]} ...")
            page.goto(f"{url}&d=ud&udvid={ids[0]}")
            page.wait_for_timeout(8_000)
            report = page.evaluate("""() => {
              const out = [];
              const sels = ['ytcp-uploads-dialog','tp-yt-paper-dialog',
                            'ytcp-dialog','[role=dialog]',
                            'ytcp-video-metadata-editor','#dialog-title',
                            '#step-badge-3','#done-button',
                            'tp-yt-paper-radio-button[name=PRIVATE]'];
              for (const s of sels) {
                const els = [...document.querySelectorAll(s)];
                out.push(s + ' -> ' + els.length + ' | visible: ' +
                  els.filter(e => { const r = e.getBoundingClientRect();
                                    return r.width > 0 && r.height > 0; }).length);
              }
              const dlg = [...document.querySelectorAll('*')]
                .filter(e => e.tagName.toLowerCase().includes('dialog'))
                .map(e => { const r = e.getBoundingClientRect();
                            return e.tagName.toLowerCase()
                                   + (r.width>0&&r.height>0 ? ' [VISIBLE '
                                      + Math.round(r.width) + 'x' + Math.round(r.height) + ']'
                                      : ' [hidden]'); });
              return out.concat(['--- every *dialog* tag on the page ---'], dlg);
            }""")
            for line in report:
                log("  " + line)
            shot = HERE / "dialog-debug.png"
            try:
                page.screenshot(path=str(shot))
                log(f"  screenshot: {shot}")
            except Exception:
                pass
            if args.hold:
                log("  Browser open. Press Enter to close.")
                try:
                    input()
                except (EOFError, KeyboardInterrupt):
                    pass
            ctx.close()
            return

        n = count_drafts(page, args.settle)
        if n == 0:
            log("  nothing on first read, reloading once...")
            page.reload()
            n = count_drafts(page, max(args.settle * 3, 8.0))
        if n == 0:
            log("No drafts found.")
            log(f"  page url:   {page.url}")
            log(f"  page title: {page.title()}")
            shot = HERE / "draft-page-debug.png"
            try:
                page.screenshot(path=str(shot), full_page=True)
                log(f"  screenshot: {shot}")
            except Exception:
                pass
            if "accounts.google" in page.url or "signin" in page.url:
                log("  -> not signed in. Run: python finish_drafts.py --login")
            else:
                log("  -> signed in, but no 'Edit draft' buttons on the page.")
                log("     Check the screenshot; if drafts ARE listed, Studio may")
                log("     still have been rendering. Try --settle 8.")
            if args.hold:
                log("\n  Browser left open. Press Enter here to close it.")
                try:
                    input()
                except (EOFError, KeyboardInterrupt):
                    pass
            ctx.close()
            return

        log(f"{n} draft(s) on this page.")
        if not args.apply:
            log(f"\nDry run — nothing changed. Add --apply to finish them "
                f"and set each {args.visibility}.")
            ctx.close()
            return

        target = args.limit if args.limit else 10_000
        done, skipped, failed = 0, [], []
        tried: set[str] = set()
        empty_reads = 0
        while done < target:
            page.goto(url)
            ids = [v for v in draft_ids(page, args.settle) if v not in tried]
            if ids:
                vid = ids[0]
                tried.add(vid)
                empty_reads = 0
            else:
                if page.locator(SEL_EDIT_DRAFT).count() == 0:
                    # Nothing visible: reload and look harder before concluding
                    # the queue is empty.
                    empty_reads += 1
                    if empty_reads >= 3:
                        break
                    page.wait_for_timeout(5_000)
                    continue
                vid = ""
            attempt_no = done + len(skipped) + len(failed) + 1
            left = len(ids) if ids else page.locator(SEL_EDIT_DRAFT).count()
            log(f"\n[{attempt_no}] ({left} left, {done} done)")
            try:
                title = finish_one(page, url, vid, args.visibility, args.patience)
                done += 1
                log(f"  {args.visibility}: {title[:70]}")
            except NotReady as e:
                skipped.append(vid)
                log(f"  not ready, skipping for now: {str(e)[:60]}")
            except Exception as e:
                msg = str(e).splitlines()[0][:160]
                failed.append(msg)
                log(f"  FAILED: {msg}")

        log(f"\n--- {done} finished, {len(skipped)} not ready, {len(failed)} failed ---")
        if skipped:
            log("Not-ready ones are still uploading or in checks. Rerun in a few minutes.")
        ctx.close()
        sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
