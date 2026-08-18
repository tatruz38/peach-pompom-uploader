#!/usr/bin/env python3
"""Peach Pompom Uploader — local web GUI.

    python server.py            # then open http://localhost:8756

Runs entirely on your own machine. Nothing is exposed to the network:
the server binds to 127.0.0.1 only.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from googleapiclient.errors import HttpError

from ytuploader import core
from ytuploader.core import Item, State, StopForToday

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"
PORT = 8756

SECRETS = HERE / "client_secret.json"
TOKEN = HERE / "token.json"
STATE = HERE / "uploaded.json"


class Job:
    """Holds the queue and drives the worker thread."""

    def __init__(self):
        self.items: list[Item] = []
        self.state = State(STATE)
        self.running = False
        self.stop_flag = False
        self.stopped_reason = ""
        self.limit = 0
        self.current = -1
        self.listeners: list[queue.Queue] = []
        self._lock = threading.Lock()

    # -- events -----------------------------------------------------------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self.listeners.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self.listeners:
                self.listeners.remove(q)

    def emit(self, kind: str, **payload) -> None:
        msg = json.dumps({"type": kind, **payload})
        with self._lock:
            dead = []
            for q in self.listeners:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self.listeners.remove(q)

    def log(self, text: str) -> None:
        self.emit("log", text=text, t=time.strftime("%H:%M:%S"))

    # -- queue ------------------------------------------------------------
    def load(self, items: list[Item]) -> None:
        for it in items:
            if self.state.done(it.file):
                it.status = "done"
                it.progress = 100
                rec = self.state.data[it.file]
                it.video_id = rec.get("video_id", "")
                it.message = "already uploaded"
            elif not Path(it.file).exists():
                it.status = "failed"
                it.message = "file not found on disk"
        self.items = items
        self.emit("queue", items=[i.dict() for i in items])

    def snapshot(self) -> dict:
        return {
            "items": [i.dict() for i in self.items],
            "running": self.running,
            "current": self.current,
            "stopped_reason": self.stopped_reason,
            "authed": core.has_token(TOKEN),
            "counts": self.counts(),
        }

    def counts(self) -> dict:
        c = {"pending": 0, "uploading": 0, "done": 0, "failed": 0}
        for i in self.items:
            c[i.status] = c.get(i.status, 0) + 1
        return c

    # -- worker -----------------------------------------------------------
    def start(self, limit: int = 0) -> None:
        if self.running:
            return
        self.limit = limit
        self.stop_flag = False
        self.stopped_reason = ""
        self.running = True
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        self.stop_flag = True
        self.log("stop requested — finishing current file first")

    def _run(self) -> None:
        try:
            self.log("authenticating…")
            youtube = core.get_service(SECRETS, TOKEN)
            self.emit("auth", authed=True)
            self.log("signed in")
        except Exception as e:
            self.log(f"sign-in failed: {e}")
            self.running = False
            self.emit("state", **self.snapshot())
            return

        uploaded = 0
        for idx, item in enumerate(self.items):
            if self.stop_flag:
                self.stopped_reason = "stopped by you"
                break
            if item.status in ("done", "failed"):
                continue
            if self.limit and uploaded >= self.limit:
                self.stopped_reason = f"reached this run's limit of {self.limit}"
                break

            self.current = idx
            item.status, item.progress, item.message = "uploading", 0, ""
            self.emit("item", index=idx, item=item.dict())
            self.log(f"[{idx + 1}/{len(self.items)}] {Path(item.file).name}")

            def prog(p: int, _idx=idx, _item=item):
                _item.progress = p
                self.emit("progress", index=_idx, progress=p)

            try:
                vid = core.upload_one(youtube, item, on_progress=prog, log=self.log)
                item.status, item.progress, item.video_id = "done", 100, vid
                item.message = "uploaded"
                self.state.record(item.file, vid, item.title or Path(item.file).stem)
                uploaded += 1
            except HttpError as e:
                reason = core.stop_reason(e)
                if reason:
                    item.status, item.message = "pending", ""
                    self.stopped_reason = f"{reason} reached — resume later"
                    self.log(f"STOP: {reason}. Nothing lost; press Start again later.")
                    self.emit("item", index=idx, item=item.dict())
                    break
                item.status, item.message = "failed", str(e)[:300]
                self.log(f"failed: {item.message}")
            except Exception as e:
                item.status, item.message = "failed", str(e)[:300]
                self.log(f"failed: {item.message}")

            self.emit("item", index=idx, item=item.dict())

        self.current = -1
        self.running = False
        if not self.stopped_reason:
            self.stopped_reason = "queue finished"
        self.log(self.stopped_reason)
        self.emit("state", **self.snapshot())


JOB = Job()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    # -- helpers ----------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            html = (WEB / "index.html").read_bytes()
            return self._send(200, html, "text/html; charset=utf-8")
        if path == "/api/state":
            return self._json(JOB.snapshot())
        if path == "/api/events":
            return self._sse()
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        data = self._body()
        try:
            if path == "/api/load-csv":
                p = Path(data.get("path", "")).expanduser()
                if not p.exists():
                    return self._json({"error": f"not found: {p}"}, 400)
                base = data.get("video_dir") or ""
                items = core.items_from_csv(p, Path(base).expanduser() if base else None)
                JOB.load(items)
                return self._json(JOB.snapshot())

            if path == "/api/load-folder":
                p = Path(data.get("path", "")).expanduser()
                if not p.is_dir():
                    return self._json({"error": f"not a folder: {p}"}, 400)
                JOB.load(core.items_from_folder(p))
                return self._json(JOB.snapshot())

            if path == "/api/settings":
                for it in JOB.items:
                    if it.status == "pending":
                        if "privacy" in data:
                            it.privacy = data["privacy"]
                        if "category" in data:
                            it.category = data["category"]
                JOB.emit("queue", items=[i.dict() for i in JOB.items])
                return self._json(JOB.snapshot())

            if path == "/api/start":
                JOB.start(int(data.get("limit") or 0))
                return self._json({"ok": True})

            if path == "/api/stop":
                JOB.stop()
                return self._json({"ok": True})

            if path == "/api/verify":
                youtube = core.get_service(SECRETS, TOKEN)
                result = core.verify(youtube, JOB.state)
                if data.get("requeue") and result["broken"]:
                    JOB.state.drop([b["key"] for b in result["broken"]])
                    for it in JOB.items:
                        if any(b["key"] == it.file for b in result["broken"]):
                            it.status, it.progress, it.message = "pending", 0, "requeued"
                    JOB.emit("queue", items=[i.dict() for i in JOB.items])
                return self._json(result)

            self._json({"error": "unknown endpoint"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    # -- server-sent events ----------------------------------------------
    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = JOB.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                    self.wfile.write(f"data: {msg}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except Exception:
            pass
        finally:
            JOB.unsubscribe(q)


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"Peach Pompom Uploader running at {url}")
    print("Press Ctrl+C to stop.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
