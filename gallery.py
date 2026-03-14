#!/usr/bin/env python3
"""
Real-time photo gallery web server for HackberryPi digicam.

Watches photo directories for new images and pushes updates to connected
browsers via Server-Sent Events (SSE). No page refresh needed.
"""

import argparse
import json
import os
import queue
import threading
import time
from pathlib import Path

from flask import Flask, Response, send_from_directory, abort
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Directories to watch (default: both camera app output dirs).
DEFAULT_DIRS = [
    Path.home() / "CreamPi",
    Path.home() / "digicam_photos",
]

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png"}

app = Flask(__name__)

# All connected SSE clients get events from this list of queues.
sse_clients: list[queue.Queue] = []
sse_clients_lock = threading.Lock()

# Watched directories (set at startup).
watch_dirs: list[Path] = []


# ---------------------------------------------------------------------------
# Filesystem watcher
# ---------------------------------------------------------------------------

class PhotoHandler(FileSystemEventHandler):
    """Notify SSE clients when a new photo appears."""

    def on_created(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() not in ALLOWED_EXTENSIONS:
            return
        # Small delay so the file is fully written before we serve it.
        threading.Timer(0.3, self._broadcast, args=(path,)).start()

    def _broadcast(self, path: Path):
        photo = _photo_info(path)
        if photo is None:
            return
        data = json.dumps(photo)
        with sse_clients_lock:
            for q in sse_clients:
                q.put(data)


def _photo_info(path: Path) -> dict | None:
    """Return JSON-serialisable info for a photo, or None if invalid."""
    if not path.is_file():
        return None
    if path.suffix.lower() not in ALLOWED_EXTENSIONS:
        return None
    stat = path.stat()
    # Build a URL-safe ID: dir_index/filename
    for i, d in enumerate(watch_dirs):
        try:
            path.relative_to(d)
            return {
                "src": f"/photo/{i}/{path.name}",
                "name": path.name,
                "mtime": stat.st_mtime,
            }
        except ValueError:
            continue
    return None


def _all_photos() -> list[dict]:
    """Scan all watched directories and return sorted photo list."""
    photos = []
    for d in watch_dirs:
        if not d.is_dir():
            continue
        for f in d.iterdir():
            info = _photo_info(f)
            if info:
                photos.append(info)
    photos.sort(key=lambda p: p["mtime"], reverse=True)
    return photos


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return GALLERY_HTML


@app.route("/api/photos")
def api_photos():
    return json.dumps(_all_photos()), 200, {"Content-Type": "application/json"}


@app.route("/photo/<int:dir_idx>/<filename>")
def serve_photo(dir_idx, filename):
    if dir_idx < 0 or dir_idx >= len(watch_dirs):
        abort(404)
    directory = watch_dirs[dir_idx]
    # Prevent path traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        abort(400)
    return send_from_directory(str(directory), filename)


@app.route("/stream")
def stream():
    """SSE endpoint — pushes new photo events to the browser."""
    q: queue.Queue = queue.Queue()
    with sse_clients_lock:
        sse_clients.append(q)

    def generate():
        try:
            while True:
                try:
                    data = q.get(timeout=30)
                    yield f"data: {data}\n\n"
                except queue.Empty:
                    # Keep-alive comment to prevent proxy/browser timeout.
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            with sse_clients_lock:
                sse_clients.remove(q)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Gallery HTML (single-page, self-contained)
# ---------------------------------------------------------------------------

GALLERY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HackberryPi Gallery</title>
<style>
  :root {
    --bg: #111;
    --card-bg: #1a1a1a;
    --accent: #e8e8e8;
    --text: #ccc;
    --radius: 6px;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: "SF Mono", "Fira Code", "Consolas", monospace;
    min-height: 100vh;
  }
  header {
    padding: 1.2rem 1.5rem;
    border-bottom: 1px solid #222;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky;
    top: 0;
    background: var(--bg);
    z-index: 100;
  }
  header h1 {
    font-size: 1.1rem;
    font-weight: 600;
    color: var(--accent);
    letter-spacing: 0.05em;
  }
  #count {
    font-size: 0.85rem;
    color: #666;
  }
  #live-dot {
    display: inline-block;
    width: 8px; height: 8px;
    background: #4f4;
    border-radius: 50%;
    margin-right: 6px;
    animation: pulse 2s infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.3; }
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: 12px;
    padding: 1rem 1.5rem 2rem;
  }
  .card {
    background: var(--card-bg);
    border-radius: var(--radius);
    overflow: hidden;
    cursor: pointer;
    transition: transform 0.15s ease, box-shadow 0.15s ease;
  }
  .card:hover {
    transform: translateY(-2px);
    box-shadow: 0 6px 20px rgba(0,0,0,0.5);
  }
  .card img {
    width: 100%;
    aspect-ratio: 16/9;
    object-fit: cover;
    display: block;
    background: #222;
  }
  .card .meta {
    padding: 8px 10px;
    font-size: 0.75rem;
    color: #888;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .card.new {
    animation: slideIn 0.4s ease;
  }
  @keyframes slideIn {
    from { opacity: 0; transform: scale(0.9) translateY(-10px); }
    to   { opacity: 1; transform: scale(1) translateY(0); }
  }

  /* Lightbox */
  #lightbox {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.92);
    z-index: 200;
    justify-content: center;
    align-items: center;
    cursor: zoom-out;
  }
  #lightbox.active { display: flex; }
  #lightbox img {
    max-width: 95vw;
    max-height: 92vh;
    object-fit: contain;
    border-radius: 4px;
  }
  #lightbox .lb-name {
    position: absolute;
    bottom: 1rem;
    left: 50%;
    transform: translateX(-50%);
    font-size: 0.8rem;
    color: #888;
    background: rgba(0,0,0,0.6);
    padding: 4px 12px;
    border-radius: 4px;
  }
  #lightbox .lb-dl {
    position: absolute;
    top: 1rem;
    right: 1rem;
    color: #aaa;
    text-decoration: none;
    font-size: 0.9rem;
    background: rgba(255,255,255,0.1);
    padding: 6px 14px;
    border-radius: 4px;
  }
  #lightbox .lb-dl:hover { background: rgba(255,255,255,0.2); }

  .empty {
    text-align: center;
    padding: 4rem 1rem;
    color: #555;
    font-size: 0.95rem;
  }
</style>
</head>
<body>

<header>
  <h1><span id="live-dot"></span>HACKBERRYPI GALLERY</h1>
  <span id="count"></span>
</header>

<div class="grid" id="grid"></div>
<div class="empty" id="empty" style="display:none">No photos yet. Start snapping!</div>

<div id="lightbox">
  <img id="lb-img" src="" alt="">
  <a id="lb-dl" class="lb-dl" href="" download>Download</a>
  <div class="lb-name" id="lb-name"></div>
</div>

<script>
const grid = document.getElementById("grid");
const countEl = document.getElementById("count");
const emptyEl = document.getElementById("empty");
const lb = document.getElementById("lightbox");
const lbImg = document.getElementById("lb-img");
const lbDl = document.getElementById("lb-dl");
const lbName = document.getElementById("lb-name");
let photoCount = 0;

function updateCount() {
  countEl.textContent = photoCount + " photo" + (photoCount !== 1 ? "s" : "");
  emptyEl.style.display = photoCount === 0 ? "block" : "none";
}

function makeCard(photo, isNew) {
  const card = document.createElement("div");
  card.className = "card" + (isNew ? " new" : "");
  card.innerHTML =
    '<img src="' + photo.src + '" loading="lazy" alt="' + photo.name + '">' +
    '<div class="meta">' + photo.name + '</div>';
  card.addEventListener("click", function() { openLightbox(photo); });
  return card;
}

function openLightbox(photo) {
  lbImg.src = photo.src;
  lbDl.href = photo.src;
  lbName.textContent = photo.name;
  lb.classList.add("active");
}

lb.addEventListener("click", function(e) {
  if (e.target === lbDl) return;
  lb.classList.remove("active");
});
document.addEventListener("keydown", function(e) {
  if (e.key === "Escape") lb.classList.remove("active");
});

// Load existing photos.
fetch("/api/photos")
  .then(function(r) { return r.json(); })
  .then(function(photos) {
    photoCount = photos.length;
    updateCount();
    photos.forEach(function(p) { grid.appendChild(makeCard(p, false)); });
  });

// SSE for real-time updates.
var es = new EventSource("/stream");
es.onmessage = function(e) {
  var photo = JSON.parse(e.data);
  photoCount++;
  updateCount();
  var card = makeCard(photo, true);
  grid.insertBefore(card, grid.firstChild);
};
es.onerror = function() {
  // EventSource auto-reconnects, nothing to do.
};
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="HackberryPi real-time photo gallery")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5000, help="Port (default: 5000)")
    parser.add_argument("--dirs", nargs="+", type=Path, default=None,
                        help="Photo directories to watch (default: ~/CreamPi ~/digicam_photos)")
    args = parser.parse_args()

    global watch_dirs
    watch_dirs = args.dirs if args.dirs else DEFAULT_DIRS

    # Ensure directories exist.
    for d in watch_dirs:
        d.mkdir(parents=True, exist_ok=True)

    # Start filesystem watcher.
    handler = PhotoHandler()
    observer = Observer()
    for d in watch_dirs:
        observer.schedule(handler, str(d), recursive=False)
    observer.start()

    print(f"Gallery serving photos from: {', '.join(str(d) for d in watch_dirs)}")
    print(f"Open http://localhost:{args.port} in your browser")

    try:
        app.run(host=args.host, port=args.port, threaded=True)
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
