#!/usr/bin/env python3
"""Run converter scripts on a schedule and expose an Ingress status page."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
from collections import Counter
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


INGRESS_PROXY_IP = "172.30.32.2"
STATUS_LABELS = {
    "pending": "Pending",
    "reviewed_pending": "Review required",
    "reviewed_process": "Approved",
    "in_progress": "In progress",
    "done": "Done",
    "done_existing": "Existing",
    "failed": "Failed",
    "other": "Other",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class ScanController:
    def __init__(
        self,
        scan_interval: int,
        movies_path: str,
        tv_path: str,
        script_dir: Path,
        log_dir: Path,
        queue_path: Path,
    ) -> None:
        self.scan_interval = scan_interval
        self.movies_path = movies_path
        self.tv_path = tv_path
        self.script_dir = script_dir
        self.log_path = log_dir / "addon.log"
        self.queue_path = queue_path
        self.trigger = threading.Event()
        self.state_lock = threading.Lock()
        self.queue_refresh_lock = threading.Lock()
        self.state = "starting"
        self.last_started: str | None = None
        self.last_finished: str | None = None
        self.last_result: str | None = None
        self.media: list[dict[str, object]] = []
        self.media_revision = 0
        self.queue_signature: tuple[int, int, int] | None = None
        self.queue_exists = False
        self.queue_error: str | None = None

    def request_scan(self) -> bool:
        already_queued = self.trigger.is_set()
        self.trigger.set()
        return not already_queued

    def status(self) -> dict[str, object]:
        self._refresh_queue()
        with self.state_lock:
            counts = Counter(str(item["status"]) for item in self.media)
            type_counts = Counter(str(item["media_type"]) for item in self.media)
            return {
                "state": self.state,
                "queued": self.trigger.is_set(),
                "scan_interval": self.scan_interval,
                "last_started": self.last_started,
                "last_finished": self.last_finished,
                "last_result": self.last_result,
                "media_revision": self.media_revision,
                "queue_path": str(self.queue_path),
                "queue_exists": self.queue_exists,
                "queue_error": self.queue_error,
                "counts": {
                    "all": len(self.media),
                    "movies": type_counts.get("Movie", 0),
                    "tv": type_counts.get("TV", 0),
                    **{key: counts.get(key, 0) for key in STATUS_LABELS},
                },
            }

    def media_list(self) -> dict[str, object]:
        self._refresh_queue()
        with self.state_lock:
            items = [dict(item) for item in self.media]
            return {"revision": self.media_revision, "items": items}

    def run_forever(self) -> None:
        while True:
            self._refresh_queue(force=True)
            self._run_scripts()
            self._refresh_queue(force=True)

            with self.state_lock:
                self.state = "waiting"
                self.last_finished = now_iso()

            triggered = self.trigger.wait(self.scan_interval)
            if triggered:
                self.trigger.clear()

    def _refresh_queue(self, force: bool = False) -> None:
        with self.queue_refresh_lock:
            try:
                stat = self.queue_path.stat()
                signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            except FileNotFoundError:
                with self.state_lock:
                    changed = self.queue_exists or bool(self.media)
                    self.queue_exists = False
                    self.queue_error = f"Queue file not found: {self.queue_path}"
                    self.queue_signature = None
                    self.media = []
                    if changed:
                        self.media_revision += 1
                return
            except OSError as exc:
                with self.state_lock:
                    self.queue_error = f"Could not inspect queue: {exc}"
                return

            with self.state_lock:
                if not force and self.queue_signature == signature:
                    return

            try:
                rows = self._read_queue_rows()
            except OSError as exc:
                with self.state_lock:
                    self.queue_exists = True
                    self.queue_error = f"Could not read queue: {exc}"
                return

            with self.state_lock:
                self.media = rows
                self.queue_exists = True
                self.queue_error = None
                self.queue_signature = signature
                self.media_revision += 1

    def _read_queue_rows(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        with self.queue_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                raw_line = raw_line.rstrip("\r\n")
                if not raw_line:
                    continue
                if "\t" in raw_line:
                    raw_status, path = raw_line.split("\t", 1)
                else:
                    raw_status, path = "MALFORMED", raw_line
                status = self._queue_status(raw_status)
                media_type, title, relative_path = self._path_details(path)
                rows.append({
                    "line": line_number,
                    "path": path,
                    "relative_path": relative_path,
                    "media_type": media_type,
                    "title": title,
                    "status": status,
                    "status_label": STATUS_LABELS[status],
                    "status_detail": raw_status,
                })
        return rows

    @staticmethod
    def _queue_status(raw_status: str) -> str:
        if raw_status == "PENDING":
            return "pending"
        if raw_status == "REVIEWED:PENDING":
            return "reviewed_pending"
        if raw_status == "REVIEWED:PROCESS":
            return "reviewed_process"
        if raw_status == "IN_PROGRESS" or raw_status.startswith("IN_PROGRESS:"):
            return "in_progress"
        if raw_status == "DONE":
            return "done"
        if raw_status == "DONE:EXISTING":
            return "done_existing"
        if raw_status == "FAILED" or raw_status.startswith("FAILED:"):
            return "failed"
        return "other"

    def _path_details(self, path: str) -> tuple[str, str, str]:
        media_path = Path(path)
        for media_type, root_value in (("TV", self.tv_path), ("Movie", self.movies_path)):
            try:
                relative = media_path.relative_to(Path(root_value))
            except ValueError:
                continue
            title = relative.parts[0] if len(relative.parts) > 1 else media_path.stem
            return media_type, title, str(relative)

        path_parts = media_path.parts
        if "tv" in path_parts:
            index = path_parts.index("tv")
            relative_parts = path_parts[index + 1 :]
            title = relative_parts[0] if len(relative_parts) > 1 else media_path.stem
            return "TV", title, str(Path(*relative_parts))
        if "movies" in path_parts:
            index = path_parts.index("movies")
            relative_parts = path_parts[index + 1 :]
            title = relative_parts[0] if len(relative_parts) > 1 else media_path.stem
            return "Movie", title, str(Path(*relative_parts))
        return "Other", media_path.stem or path, path

    def _run_scripts(self) -> None:
        with self.state_lock:
            self.state = "running"
            self.last_started = now_iso()
            self.last_result = None

        env = os.environ.copy()
        env["MOVIES_PATH"] = self.movies_path
        env["TV_PATH"] = self.tv_path
        scripts_failed = False

        with self.log_path.open("a", encoding="utf-8") as log:
            print(f"[INFO] {datetime.now().astimezone().ctime()} running scripts", file=log, flush=True)
            for script in sorted(self.script_dir.glob("*.sh")):
                if not os.access(script, os.X_OK):
                    continue
                print(f"[INFO] Running {script}", file=log, flush=True)
                try:
                    result = subprocess.run(
                        [str(script)],
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        check=False,
                    )
                    return_code = result.returncode
                except OSError as exc:
                    print(f"[ERROR] Could not run {script}: {exc}", file=log, flush=True)
                    return_code = 1

                if return_code != 0:
                    scripts_failed = True

        with self.state_lock:
            self.last_result = "failed" if scripts_failed else "success"


HTML_PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Jellyfin Direct Play Converter</title>
  <style>
    :root { color-scheme: light dark; font-family: system-ui, sans-serif; --bg: var(--primary-background-color, #111318); --card: var(--card-background-color, #1b1e25); --text: var(--primary-text-color, #edf1f7); --muted: var(--secondary-text-color, #9da8b8); --line: rgba(145,158,180,.22); --primary: var(--primary-color, #03a9f4); }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; background: var(--bg); color: var(--text); }
    main { min-height: 100vh; display: grid; grid-template-rows: auto auto 1fr; }
    header { padding: 24px; display: flex; gap: 20px; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--line); }
    h1 { margin: 0; font-size: 1.45rem; }
    .sub { margin-top: 6px; color: var(--muted); }
    button, select, input { font: inherit; }
    button { padding: 9px 14px; border: 1px solid var(--line); border-radius: 7px; background: transparent; color: var(--text); cursor: pointer; }
    button:hover { border-color: var(--primary); }
    button.primary { border-color: var(--primary); background: var(--primary); color: #fff; font-weight: 650; }
    button:disabled { cursor: wait; opacity: .6; }
    .summary { padding: 12px 24px; display: flex; gap: 8px; flex-wrap: wrap; border-bottom: 1px solid var(--line); }
    .pill { padding: 5px 10px; border: 1px solid var(--line); border-radius: 999px; color: var(--muted); font-size: .86rem; }
    .pill strong { color: var(--text); }
    .content { min-height: 0; display: grid; grid-template-rows: auto 1fr; }
    .toolbar { padding: 12px 24px; display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
    .toolbar input { margin-left: auto; min-width: 260px; }
    select, input { padding: 8px 10px; border: 1px solid var(--line); border-radius: 7px; background: var(--card); color: var(--text); }
    .table-wrap { min-height: 0; overflow: auto; border-top: 1px solid var(--line); }
    table { width: 100%; border-collapse: collapse; font-size: .9rem; }
    th { position: sticky; top: 0; z-index: 1; padding: 10px 12px; text-align: left; background: var(--card); color: var(--muted); border-bottom: 1px solid var(--line); }
    td { padding: 10px 12px; border-bottom: 1px solid var(--line); vertical-align: top; }
    tr:hover td { background: rgba(127,143,166,.08); }
    .type, .status { display: inline-block; white-space: nowrap; padding: 3px 8px; border-radius: 999px; font-size: .78rem; font-weight: 700; }
    .type { background: #536dfe22; color: #91a1ff; }
    .pending { background: #ff980022; color: #ffb74d; }
    .reviewed_pending { background: #ffc10722; color: #ffd54f; }
    .reviewed_process { background: #7e57c222; color: #b39ddb; }
    .in_progress { background: #03a9f422; color: #4fc3f7; }
    .done, .done_existing { background: #4caf5022; color: #81c784; }
    .failed, .other { background: #f4433622; color: #ef9a9a; }
    .title { font-weight: 650; }
    .path { max-width: 680px; color: var(--muted); overflow-wrap: anywhere; }
    .empty { padding: 36px 24px; text-align: center; color: var(--muted); }
    #message { min-height: 1.3em; color: var(--muted); }
    @media (max-width: 760px) { header { align-items: flex-start; flex-direction: column; } .toolbar input { order: 3; margin-left: 0; min-width: 100%; width: 100%; } .path-column { display: none; } }
  </style>
</head>
<body>
  <main>
    <header>
      <div><h1>Jellyfin Direct Play Converter</h1><div class="sub" id="meta">Loading worker status…</div><div class="sub" id="message" role="status"></div></div>
      <button id="run" class="primary" type="button">Run now</button>
    </header>
    <div class="summary" id="summary"></div>
    <div class="content">
      <div class="toolbar">
        <select id="status-filter" aria-label="Filter by status">
          <option value="all">All statuses</option><option value="pending">Pending</option><option value="reviewed_pending">Review required</option><option value="reviewed_process">Approved</option><option value="in_progress">In progress</option><option value="done">Done</option><option value="done_existing">Existing</option><option value="failed">Failed</option><option value="other">Other</option>
        </select>
        <select id="type-filter" aria-label="Filter by media type"><option value="all">All media</option><option value="Movie">Movies</option><option value="TV">TV</option><option value="Other">Other</option></select>
        <input id="search" type="search" placeholder="Search titles or paths" aria-label="Search titles or paths">
      </div>
      <div class="table-wrap">
        <table><thead><tr><th>Line</th><th>Type</th><th>Title</th><th>Status</th><th class="path-column">Path</th></tr></thead><tbody id="rows"></tbody></table>
        <div class="empty" id="empty" hidden>No queue rows match the current filters.</div>
      </div>
    </div>
  </main>
  <script>
    const basePath = __BASE_PATH__;
    const state = { items: [], revision: -1 };
    const button = document.querySelector('#run');
    const message = document.querySelector('#message');
    function escapeHtml(value) { return String(value).replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'})[char]); }
    function displayTime(value) { return value ? new Date(value).toLocaleString() : 'never'; }
    async function fetchJson(endpoint, options = {}) { const response = await fetch(basePath + endpoint, { cache: 'no-store', ...options }); if (!response.ok) throw new Error(`${response.status} ${response.statusText}`); return await response.json(); }
    function renderSummary(counts) {
      const values = [['Rows',counts.all],['Movies',counts.movies],['TV',counts.tv],['Pending',counts.pending],['Review',counts.reviewed_pending],['Approved',counts.reviewed_process],['Running',counts.in_progress],['Done',counts.done],['Existing',counts.done_existing],['Failed',counts.failed]];
      document.querySelector('#summary').innerHTML = values.map(([label,value]) => `<span class="pill">${escapeHtml(label)}: <strong>${escapeHtml(value || 0)}</strong></span>`).join('');
    }
    function renderRows() {
      const status = document.querySelector('#status-filter').value, type = document.querySelector('#type-filter').value, query = document.querySelector('#search').value.trim().toLowerCase();
      const visible = state.items.filter(item => (status === 'all' || item.status === status) && (type === 'all' || item.media_type === type) && (!query || item.title.toLowerCase().includes(query) || item.path.toLowerCase().includes(query)));
      document.querySelector('#empty').hidden = visible.length > 0;
      document.querySelector('#rows').innerHTML = visible.map(item => `<tr><td>${escapeHtml(item.line)}</td><td><span class="type">${escapeHtml(item.media_type)}</span></td><td><span class="title">${escapeHtml(item.title)}</span><br><span class="path">${escapeHtml(item.relative_path)}</span></td><td><span class="status ${escapeHtml(item.status)}" title="${escapeHtml(item.status_detail)}">${escapeHtml(item.status_label)}</span></td><td class="path path-column">${escapeHtml(item.path)}</td></tr>`).join('');
    }
    async function updateStatus() {
      const status = await fetchJson('api/status'), queued = status.queued ? ' · run queued' : '', result = status.last_result ? ` · last result ${status.last_result}` : '';
      const queueState = status.queue_exists ? `${status.counts.all} queue rows` : 'queue unavailable';
      document.querySelector('#meta').textContent = `Worker ${status.state}${queued} · ${queueState} · every ${status.scan_interval}s · last started ${displayTime(status.last_started)}${result}`;
      if (status.queue_error) message.textContent = status.queue_error;
      renderSummary(status.counts);
      if (status.media_revision !== state.revision) { const media = await fetchJson('api/media'); state.items = media.items; state.revision = media.revision; renderRows(); }
    }
    button.addEventListener('click', async () => { button.disabled = true; message.textContent = 'Requesting scan…'; try { const result = await fetchJson('api/run', { method: 'POST' }); message.textContent = result.queued ? 'Scan queued.' : 'A scan is already queued.'; await updateStatus(); } catch (error) { message.textContent = `Run request failed: ${error.message}`; } finally { button.disabled = false; } });
    document.querySelector('#status-filter').addEventListener('change', renderRows); document.querySelector('#type-filter').addEventListener('change', renderRows); document.querySelector('#search').addEventListener('input', renderRows);
    updateStatus().catch(error => message.textContent = `Status unavailable: ${error.message}`); setInterval(() => updateStatus().catch(() => {}), 2000);
  </script>
</body>
</html>
"""


def page(ingress_path: str) -> bytes:
    base_path = ingress_path.rstrip("/") + "/" if ingress_path else "/"
    encoded_base_path = json.dumps(base_path).replace("<", "\\u003c")
    return HTML_PAGE.replace("__BASE_PATH__", encoded_base_path).encode()


def make_handler(controller: ScanController) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _allowed(self) -> bool:
            if self.client_address[0] == INGRESS_PROXY_IP:
                return True
            self.send_error(HTTPStatus.FORBIDDEN)
            return False

        def do_GET(self) -> None:  # noqa: N802 - HTTP handler API
            if not self._allowed():
                return
            path = urlsplit(self.path).path.rstrip("/")
            if path.endswith("/api/status"):
                self._json(HTTPStatus.OK, controller.status())
                return
            if path.endswith("/api/media"):
                self._json(HTTPStatus.OK, controller.media_list())
                return
            if path in ("", "/"):
                body = page(self.headers.get("X-Ingress-Path", ""))
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802 - HTTP handler API
            if not self._allowed():
                return
            if urlsplit(self.path).path.rstrip("/").endswith("/api/run"):
                queued = controller.request_scan()
                self._json(HTTPStatus.ACCEPTED, {"queued": queued})
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def _json(self, status: HTTPStatus, payload: object) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            print(f"[INFO] Web UI: {format % args}")

    return Handler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scan-interval", type=int, required=True)
    parser.add_argument("--movies-path", required=True)
    parser.add_argument("--tv-path", required=True)
    parser.add_argument("--script-dir", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--queue-path", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    controller = ScanController(
        args.scan_interval,
        args.movies_path,
        args.tv_path,
        args.script_dir,
        args.log_dir,
        args.queue_path,
    )
    server = ThreadingHTTPServer(("0.0.0.0", 8099), make_handler(controller))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("[INFO] Status Web UI listening on port 8099")
    controller.run_forever()


if __name__ == "__main__":
    main()
