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
VIDEO_SUFFIXES = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}
STATUS_LABELS = {
    "needs_conversion": "Needs conversion",
    "converting": "Converting",
    "converted": "Converted",
    "no_ac3": "No AC3",
    "no_audio": "No audio",
    "probe_failed": "Probe failed",
    "failed": "Failed",
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
        cache_path: Path,
    ) -> None:
        self.scan_interval = scan_interval
        self.movies_path = movies_path
        self.tv_path = tv_path
        self.script_dir = script_dir
        self.log_path = log_dir / "addon.log"
        self.cache_path = cache_path
        self.trigger = threading.Event()
        self.state_lock = threading.Lock()
        self.state = "starting"
        self.last_started: str | None = None
        self.last_finished: str | None = None
        self.last_result: str | None = None
        self.media: dict[str, dict[str, object]] = self._load_cache()
        self.media_revision = 1 if self.media else 0

    def request_scan(self) -> bool:
        already_queued = self.trigger.is_set()
        self.trigger.set()
        return not already_queued

    def status(self) -> dict[str, object]:
        with self.state_lock:
            counts = Counter(str(item["status"]) for item in self.media.values())
            type_counts = Counter(str(item["media_type"]) for item in self.media.values())
            return {
                "state": self.state,
                "queued": self.trigger.is_set(),
                "scan_interval": self.scan_interval,
                "last_started": self.last_started,
                "last_finished": self.last_finished,
                "last_result": self.last_result,
                "media_revision": self.media_revision,
                "counts": {
                    "all": len(self.media),
                    "movies": type_counts.get("Movie", 0),
                    "tv": type_counts.get("TV", 0),
                    **{key: counts.get(key, 0) for key in STATUS_LABELS},
                },
            }

    def media_list(self) -> dict[str, object]:
        with self.state_lock:
            public_keys = (
                "path",
                "relative_path",
                "media_type",
                "title",
                "codec",
                "status",
                "status_label",
            )
            items = sorted(
                ({key: item[key] for key in public_keys} for item in self.media.values()),
                key=lambda item: (
                    str(item["media_type"]),
                    str(item["title"]).casefold(),
                    str(item["path"]).casefold(),
                ),
            )
            return {"revision": self.media_revision, "items": items}

    def run_forever(self) -> None:
        while True:
            self._discover_media()
            failed_paths = self._run_scripts()
            self._discover_media(failed_paths)

            with self.state_lock:
                self.state = "waiting"
                self.last_finished = now_iso()

            triggered = self.trigger.wait(self.scan_interval)
            if triggered:
                self.trigger.clear()

    def _discover_media(self, failed_paths: set[str] | None = None) -> None:
        with self.state_lock:
            self.state = "scanning"
            cached = dict(self.media)

        discovered: dict[str, dict[str, object]] = {}
        roots = (("TV", self.tv_path), ("Movie", self.movies_path))
        for media_type, root_value in roots:
            root = Path(root_value)
            if not root.is_dir():
                continue
            for directory, _, filenames in os.walk(root):
                for filename in filenames:
                    path = Path(directory) / filename
                    if path.suffix.casefold() not in VIDEO_SUFFIXES or filename.endswith(".tmp"):
                        continue
                    try:
                        stat = path.stat()
                        relative = path.relative_to(root)
                    except (FileNotFoundError, OSError, ValueError):
                        continue

                    path_text = str(path)
                    previous = cached.get(path_text)
                    if (
                        previous
                        and previous.get("mtime_ns") == stat.st_mtime_ns
                        and previous.get("size") == stat.st_size
                        and previous.get("codec") is not None
                    ):
                        codec = str(previous["codec"])
                        probe_error = bool(previous.get("probe_error"))
                    else:
                        codec, probe_error = self._probe_audio(path)

                    status = self._media_status(codec, probe_error)
                    if failed_paths and path_text in failed_paths:
                        status = "failed"
                    title = relative.parts[0] if len(relative.parts) > 1 else path.stem
                    discovered[path_text] = {
                        "path": path_text,
                        "relative_path": str(relative),
                        "media_type": media_type,
                        "title": title,
                        "codec": codec,
                        "status": status,
                        "status_label": STATUS_LABELS[status],
                        "mtime_ns": stat.st_mtime_ns,
                        "size": stat.st_size,
                        "probe_error": probe_error,
                    }

        with self.state_lock:
            self.media = discovered
            self.media_revision += 1
        self._save_cache(discovered)

    def _probe_audio(self, path: Path) -> tuple[str, bool]:
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-select_streams", "a:0",
                    "-show_entries", "stream=codec_name",
                    "-of", "default=noprint_wrappers=1:nokey=1", str(path),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "unknown", True

        codec = result.stdout.strip().splitlines()
        if result.returncode != 0:
            return "unknown", True
        return (codec[0].casefold() if codec else "", False)

    @staticmethod
    def _media_status(codec: str, probe_error: bool) -> str:
        if probe_error:
            return "probe_failed"
        if not codec:
            return "no_audio"
        if codec == "ac3":
            return "needs_conversion"
        return "no_ac3"

    def _run_scripts(self) -> set[str]:
        with self.state_lock:
            self.state = "running"
            self.last_started = now_iso()
            self.last_result = None

        env = os.environ.copy()
        env["MOVIES_PATH"] = self.movies_path
        env["TV_PATH"] = self.tv_path
        failed_paths: set[str] = set()
        scripts_failed = False

        with self.log_path.open("a", encoding="utf-8") as log:
            print(f"[INFO] {datetime.now().astimezone().ctime()} running scripts", file=log, flush=True)
            for script in sorted(self.script_dir.glob("*.sh")):
                if not os.access(script, os.X_OK):
                    continue
                print(f"[INFO] Running {script}", file=log, flush=True)
                active_path: str | None = None
                try:
                    process = subprocess.Popen(
                        [str(script)],
                        env=env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        errors="replace",
                        bufsize=1,
                    )
                    assert process.stdout is not None
                    for line in process.stdout:
                        log.write(line)
                        log.flush()
                        if "converting: " in line:
                            active_path = line.split("converting: ", 1)[1].strip()
                            self._set_media_status(active_path, "converting")
                        elif "done: " in line:
                            if active_path:
                                self._set_media_status(active_path, "converted")
                            active_path = None
                    return_code = process.wait()
                except OSError as exc:
                    print(f"[ERROR] Could not run {script}: {exc}", file=log, flush=True)
                    return_code = 1

                if return_code != 0:
                    scripts_failed = True
                    if active_path:
                        failed_paths.add(active_path)

        with self.state_lock:
            self.last_result = "failed" if scripts_failed else "success"
        return failed_paths

    def _set_media_status(self, path: str, status: str) -> None:
        with self.state_lock:
            item = self.media.get(path)
            if not item:
                return
            item["status"] = status
            item["status_label"] = STATUS_LABELS[status]
            self.media_revision += 1

    def _load_cache(self) -> dict[str, dict[str, object]]:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            items = payload.get("items", [])
            if not isinstance(items, list):
                return {}
            required = {
                "path",
                "relative_path",
                "media_type",
                "title",
                "codec",
                "status",
                "status_label",
            }
            return {
                str(item["path"]): item
                for item in items
                if isinstance(item, dict) and required.issubset(item)
            }
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return {}

    def _save_cache(self, media: dict[str, dict[str, object]]) -> None:
        temp_path = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path.write_text(json.dumps({"items": list(media.values())}), encoding="utf-8")
            temp_path.replace(self.cache_path)
        except OSError as exc:
            print(f"[WARNING] Could not save media status cache: {exc}")


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
    .needs_conversion { background: #ff980022; color: #ffb74d; }
    .converting { background: #03a9f422; color: #4fc3f7; }
    .converted { background: #8bc34a22; color: #aed581; }
    .no_ac3 { background: #4caf5022; color: #81c784; }
    .no_audio { background: #78909c22; color: #b0bec5; }
    .probe_failed, .failed { background: #f4433622; color: #ef9a9a; }
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
          <option value="all">All statuses</option><option value="needs_conversion">Needs conversion</option><option value="converting">Converting</option><option value="converted">Converted</option><option value="no_ac3">No AC3</option><option value="failed">Failed</option><option value="probe_failed">Probe failed</option><option value="no_audio">No audio</option>
        </select>
        <select id="type-filter" aria-label="Filter by media type"><option value="all">Movies + TV</option><option value="Movie">Movies</option><option value="TV">TV</option></select>
        <input id="search" type="search" placeholder="Search titles or paths" aria-label="Search titles or paths">
      </div>
      <div class="table-wrap">
        <table><thead><tr><th>Type</th><th>Title</th><th>Status</th><th>Audio</th><th class="path-column">Path</th></tr></thead><tbody id="rows"></tbody></table>
        <div class="empty" id="empty" hidden>No media matches the current filters.</div>
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
      const values = [['Files',counts.all],['Movies',counts.movies],['TV',counts.tv],['Needs conversion',counts.needs_conversion],['Converting',counts.converting],['Converted',counts.converted],['No AC3',counts.no_ac3],['Failed',counts.failed + counts.probe_failed]];
      document.querySelector('#summary').innerHTML = values.map(([label,value]) => `<span class="pill">${escapeHtml(label)}: <strong>${escapeHtml(value || 0)}</strong></span>`).join('');
    }
    function renderRows() {
      const status = document.querySelector('#status-filter').value, type = document.querySelector('#type-filter').value, query = document.querySelector('#search').value.trim().toLowerCase();
      const visible = state.items.filter(item => (status === 'all' || item.status === status) && (type === 'all' || item.media_type === type) && (!query || item.title.toLowerCase().includes(query) || item.path.toLowerCase().includes(query)));
      document.querySelector('#empty').hidden = visible.length > 0;
      document.querySelector('#rows').innerHTML = visible.map(item => `<tr><td><span class="type">${escapeHtml(item.media_type)}</span></td><td><span class="title">${escapeHtml(item.title)}</span><br><span class="path">${escapeHtml(item.relative_path)}</span></td><td><span class="status ${escapeHtml(item.status)}">${escapeHtml(item.status_label)}</span></td><td>${escapeHtml(item.codec || '—')}</td><td class="path path-column">${escapeHtml(item.path)}</td></tr>`).join('');
    }
    async function updateStatus() {
      const status = await fetchJson('api/status'), queued = status.queued ? ' · run queued' : '', result = status.last_result ? ` · last result ${status.last_result}` : '';
      document.querySelector('#meta').textContent = `Worker ${status.state}${queued} · every ${status.scan_interval}s · last started ${displayTime(status.last_started)}${result}`;
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
    parser.add_argument("--cache-path", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    controller = ScanController(
        args.scan_interval,
        args.movies_path,
        args.tv_path,
        args.script_dir,
        args.log_dir,
        args.cache_path,
    )
    server = ThreadingHTTPServer(("0.0.0.0", 8099), make_handler(controller))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("[INFO] Status Web UI listening on port 8099")
    controller.run_forever()


if __name__ == "__main__":
    main()
