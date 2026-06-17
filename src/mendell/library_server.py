"""A tiny local web UI for the sample library.

``mendell library serve`` starts this — a stdlib-only HTTP server (no new deps)
that serves a single ``library.html`` page plus a small JSON API backed by the
same functions ``mendell library ...`` uses. It lets you browse/search every
registered library in the browser, audition samples (the browser ``<audio>``
element plays them via the ``/api/audio`` byte-range endpoint), re-scan folders,
and unregister libraries.

It's read-mostly + the few mutating ops the CLI already exposes (scan/remove);
sample audio is never modified, and nothing here touches a project.
"""

from __future__ import annotations

import json
import mimetypes
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config as config_mod
from . import library as library_mod
from .errors import MendellError
from .recognize import list_backends


def _json_bytes(obj: object) -> bytes:
    return json.dumps(obj).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    # Quieter than the default per-request stderr logging.
    def log_message(self, *args):  # noqa: D401, ANN002
        pass

    # -- helpers ----------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, obj: object, status: int = 200):
        self._send(status, _json_bytes(obj), "application/json")

    def _error(self, err: Exception):
        if isinstance(err, MendellError):
            self._send_json({"ok": False, "error": str(err), "code": err.code}, status=400)
        else:
            self._send_json({"ok": False, "error": str(err), "code": 3}, status=500)

    # -- routing ----------------------------------------------------------
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html", "/library.html"):
                self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/backends":
                self._send_json({"ok": True, "data": {
                    "backends": list_backends(),
                    "default": config_mod.library_recognizer_default(),
                }})
            elif path == "/api/libraries":
                self._send_json({"ok": True, "data": library_mod.list_entries()})
            elif path == "/api/search":
                self._send_json({"ok": True, "data": self._do_search(query)})
            elif path == "/api/audio":
                self._serve_audio(query)
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as err:  # noqa: BLE001
            self._error(err)

    do_HEAD = do_GET

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if parsed.path == "/api/scan":
                data = library_mod.scan(payload.get("name") or None)
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/remove":
                data = library_mod.remove(payload["name"])
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/add":
                # recognize: a backend name, "" (filename-only), or "__default__"
                # (honor the library.recognizer config setting, like the CLI).
                recognize = payload.get("recognize", "__default__")
                if recognize == "__default__":
                    recognize = config_mod.library_recognizer_default()
                elif recognize == "":
                    recognize = None
                # For the ACE-Step captioner, let the UI pick the in-flight
                # quantization mode (full/8bit/4bit) by setting the env var the
                # captioner reads. Note: the captioner is a process singleton,
                # so this takes effect on the first ace-step add and the model
                # stays loaded at that mode for the rest of the server's life.
                if recognize == "ace-step":
                    load = (payload.get("captioner_load") or "").strip()
                    if load:
                        os.environ["ACESTEP_CAPTIONER_LOAD"] = load
                data = library_mod.add(
                    payload["name"], payload["path"],
                    tags=[t.strip() for t in (payload.get("tags") or "").split(",") if t.strip()] or None,
                    analyze=bool(payload.get("analyze")),
                    recognize=recognize,
                )
                self._send_json({"ok": True, "data": data})
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as err:  # noqa: BLE001
            self._error(err)

    # -- endpoints --------------------------------------------------------
    def _do_search(self, q: dict) -> dict:
        def one(key):
            v = q.get(key, [None])[0]
            return v or None

        bpm = one("bpm")
        return library_mod.search(
            one("query"),
            library=one("library"),
            tag=one("tag"),
            category=one("category"),
            kind=one("kind"),
            instrument=one("instrument"),
            bpm=float(bpm) if bpm else None,
        )

    def _serve_audio(self, q: dict):
        ref = q.get("ref", [None])[0]
        if not ref:
            self._send(400, b"missing ref", "text/plain")
            return
        target = library_mod.resolve_ref(ref)
        if target is None or not target.is_file():
            self._send(404, b"audio not found", "text/plain")
            return

        data = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        total = len(data)
        range_header = self.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            start_s, _, end_s = range_header[len("bytes="):].partition("-")
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else total - 1
            end = min(end, total - 1)
            chunk = data[start:end + 1]
            self._send(
                206, chunk, ctype,
                extra={
                    "Content-Range": f"bytes {start}-{end}/{total}",
                    "Accept-Ranges": "bytes",
                },
            )
        else:
            self._send(200, data, ctype, extra={"Accept-Ranges": "bytes"})


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    """Run the library web UI until interrupted (Ctrl-C)."""
    httpd = ThreadingHTTPServer((host, port), _Handler)
    url = f"http://{host}:{port}/"
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    print(f"Mendell library UI at {url}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        httpd.server_close()


_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mendell — Sample Library</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.4 system-ui, sans-serif; background: #14161a; color: #e6e8eb; }
  header { padding: 16px 20px; border-bottom: 1px solid #2a2e35; display: flex; align-items: center; gap: 16px; }
  h1 { font-size: 18px; margin: 0; font-weight: 600; }
  main { display: grid; grid-template-columns: 240px 1fr; min-height: calc(100vh - 57px); }
  aside { border-right: 1px solid #2a2e35; padding: 16px; }
  section { padding: 16px 20px; }
  .lib { padding: 8px 10px; border-radius: 6px; cursor: pointer; display: flex; justify-content: space-between; align-items: center; }
  .lib:hover { background: #1d2026; }
  .lib.active { background: #2b3140; }
  .lib small { color: #8b93a1; }
  .lib .x { color: #7a818d; padding: 0 4px; }
  .lib .x:hover { color: #ff6b6b; }
  .filters { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
  input, select, button { background: #1d2026; color: #e6e8eb; border: 1px solid #333842; border-radius: 6px; padding: 7px 10px; font: inherit; }
  button { cursor: pointer; }
  button:hover { background: #262b34; }
  button.primary { background: #3b5bdb; border-color: #3b5bdb; }
  button.primary:hover { background: #4666e8; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid #23262d; white-space: nowrap; }
  th { color: #8b93a1; font-weight: 500; position: sticky; top: 0; background: #14161a; }
  td.ref { white-space: normal; word-break: break-all; max-width: 360px; }
  .tag { display: inline-block; background: #232834; color: #aab3c2; border-radius: 4px; padding: 1px 6px; margin: 0 2px 2px 0; font-size: 12px; }
  .play { width: 30px; height: 30px; border-radius: 50%; padding: 0; }
  .play.on { background: #2f9e44; border-color: #2f9e44; }
  .muted { color: #8b93a1; }
  .count { color: #8b93a1; margin-left: auto; }
  .empty { color: #8b93a1; padding: 30px; text-align: center; }
  dialog { background: #1b1e24; color: #e6e8eb; border: 1px solid #333842; border-radius: 10px; padding: 20px; width: 360px; }
  dialog h3 { margin: 0 0 12px; }
  dialog label { display: block; margin: 10px 0 4px; color: #aab3c2; }
  dialog input { width: 100%; }
  dialog .row { display: flex; gap: 8px; justify-content: flex-end; margin-top: 16px; }
</style>
</head>
<body>
<header>
  <h1>🎛 Mendell Library</h1>
  <button onclick="rescan()">Re-scan all</button>
  <button class="primary" onclick="addDlg.showModal()">+ Add folder</button>
</header>
<main>
  <aside>
    <div id="libs"></div>
  </aside>
  <section>
    <div class="filters">
      <input id="q" placeholder="search filename…" oninput="debouncedSearch()">
      <select id="category" onchange="search()"><option value="">any category</option></select>
      <select id="kind" onchange="search()">
        <option value="">any kind</option><option>loop</option><option>one-shot</option><option>unknown</option>
      </select>
      <input id="bpm" type="number" placeholder="bpm" style="width:80px" oninput="debouncedSearch()">
      <input id="instrument" placeholder="instrument" style="width:110px" oninput="debouncedSearch()">
      <span class="count" id="count"></span>
    </div>
    <div id="results"></div>
  </section>
</main>

<dialog id="addDlg">
  <h3>Register a sample folder</h3>
  <label>Name</label><input id="addName" placeholder="my-drums">
  <label>Folder path</label><input id="addPath" placeholder="/home/you/samples/drums">
  <label>Tags (comma-separated)</label><input id="addTags" placeholder="drums,lofi">
  <label>Sound recognition</label>
  <select id="addRecognize" style="width:100%" onchange="onRecognizeChange()"></select>
  <div id="captionerLoadRow" style="display:none;margin-top:10px">
    <label>ACE-Step captioner VRAM (in-flight quantization)</label>
    <select id="addCaptionerLoad" style="width:100%">
      <option value="full">full — fp16, ~22 GB VRAM</option>
      <option value="8bit">8bit — ~11 GB VRAM (needs bitsandbytes)</option>
      <option value="4bit">4bit — ~6–7 GB VRAM (needs bitsandbytes)</option>
    </select>
    <small style="opacity:.7">Applied on the first ace-step add; the model stays loaded at that mode.</small>
  </div>
  <label style="display:flex;align-items:center;gap:8px;margin-top:12px;cursor:pointer">
    <input type="checkbox" id="addAnalyze" style="width:auto"> Analyze BPM of loops (slower)
  </label>
  <div class="row">
    <button onclick="addDlg.close()">Cancel</button>
    <button class="primary" onclick="doAdd()">Add</button>
  </div>
</dialog>

<audio id="player"></audio>
<script>
let activeLib = "";
let curBtn = null;
const player = document.getElementById("player");
player.onended = () => { if (curBtn) { curBtn.classList.remove("on"); curBtn.textContent="▶"; curBtn=null; } };

async function api(url, opts) {
  const r = await fetch(url, opts);
  const j = await r.json();
  if (!j.ok) throw new Error(j.error || "request failed");
  return j.data;
}

async function loadLibs() {
  const { libraries } = await api("/api/libraries");
  const el = document.getElementById("libs");
  let html = `<div class="lib ${activeLib===""?"active":""}" onclick="selectLib('')">All libraries</div>`;
  for (const l of libraries) {
    html += `<div class="lib ${activeLib===l.name?"active":""}" onclick="selectLib('${l.name}')">
      <span>${l.name}<br><small>${l.file_count} files</small></span>
      <span class="x" title="unregister" onclick="event.stopPropagation();removeLib('${l.name}')">✕</span>
    </div>`;
  }
  el.innerHTML = html;
}

function selectLib(name) { activeLib = name; loadLibs(); search(); }

let t;
function debouncedSearch() { clearTimeout(t); t = setTimeout(search, 250); }

async function search() {
  const p = new URLSearchParams();
  if (activeLib) p.set("library", activeLib);
  const q = document.getElementById("q").value.trim();
  const cat = document.getElementById("category").value;
  const kind = document.getElementById("kind").value;
  const bpm = document.getElementById("bpm").value;
  const inst = document.getElementById("instrument").value.trim();
  if (q) p.set("query", q);
  if (cat) p.set("category", cat);
  if (kind) p.set("kind", kind);
  if (bpm) p.set("bpm", bpm);
  if (inst) p.set("instrument", inst);
  const data = await api("/api/search?" + p.toString());
  render(data.matches);
  document.getElementById("count").textContent = data.count + " samples";
  populateCategories(data.matches);
}

let categoriesSeen = new Set();
function populateCategories(matches) {
  for (const m of matches) if (m.category) categoriesSeen.add(m.category);
  const sel = document.getElementById("category");
  const cur = sel.value;
  sel.innerHTML = '<option value="">any category</option>' +
    [...categoriesSeen].sort().map(c => `<option ${c===cur?"selected":""}>${c}</option>`).join("");
}

function render(matches) {
  const el = document.getElementById("results");
  if (!matches.length) { el.innerHTML = '<div class="empty">No matching samples.</div>'; return; }
  let html = `<table><thead><tr><th></th><th>ref</th><th>category</th><th>kind</th><th>bpm</th><th>dur</th><th>instruments</th><th>tags</th></tr></thead><tbody>`;
  for (const m of matches) {
    const ref = m.ref.replace(/'/g, "\\'");
    html += `<tr>
      <td><button class="play" onclick="play(this,'${ref}')">▶</button></td>
      <td class="ref">${m.ref}</td>
      <td>${m.category||""}</td>
      <td class="muted">${m.kind||""}</td>
      <td>${m.bpm?Math.round(m.bpm):""}</td>
      <td class="muted">${m.duration?m.duration.toFixed(1)+"s":""}</td>
      <td>${(m.instruments||[]).map(i=>`<span class="tag">${i}</span>`).join("")}</td>
      <td>${(m.tags||[]).map(x=>`<span class="tag">${x}</span>`).join("")}</td>
    </tr>`;
  }
  el.innerHTML = html + "</tbody></table>";
}

function play(btn, ref) {
  if (curBtn === btn) { player.pause(); btn.classList.remove("on"); btn.textContent="▶"; curBtn=null; return; }
  if (curBtn) { curBtn.classList.remove("on"); curBtn.textContent="▶"; }
  player.src = "/api/audio?ref=" + encodeURIComponent(ref);
  player.play();
  btn.classList.add("on"); btn.textContent="⏸"; curBtn = btn;
}

async function rescan() {
  await api("/api/scan", { method:"POST", body:"{}" });
  await loadLibs(); search();
}
async function removeLib(name) {
  if (!confirm(`Unregister '${name}'? (files on disk are left untouched)`)) return;
  await api("/api/remove", { method:"POST", body: JSON.stringify({name}) });
  if (activeLib === name) activeLib = "";
  loadLibs(); search();
}
let recognizeDefault = null;
async function loadBackends() {
  const { backends, default: def } = await api("/api/backends");
  recognizeDefault = def;
  const sel = document.getElementById("addRecognize");
  const defLabel = def ? `config default (${def})` : "config default (none)";
  let html = `<option value="__default__">${defLabel}</option>`;
  html += `<option value="">filename only (no recognition)</option>`;
  html += backends.map(b => `<option value="${b}">${b}</option>`).join("");
  sel.innerHTML = html;
  onRecognizeChange();
}
function onRecognizeChange() {
  // Show the captioner VRAM picker only when the effective backend is ace-step
  // (selected directly, or via "config default" resolving to it).
  let val = document.getElementById("addRecognize").value;
  if (val === "__default__") val = recognizeDefault;
  document.getElementById("captionerLoadRow").style.display =
    (val === "ace-step") ? "block" : "none";
}

async function doAdd() {
  try {
    await api("/api/add", { method:"POST", body: JSON.stringify({
      name: addName.value.trim(), path: addPath.value.trim(), tags: addTags.value,
      recognize: document.getElementById("addRecognize").value,
      captioner_load: document.getElementById("addCaptionerLoad").value,
      analyze: document.getElementById("addAnalyze").checked,
    })});
    addDlg.close(); addName.value=addPath.value=addTags.value="";
    loadLibs(); search();
  } catch(e) { alert(e.message); }
}

loadBackends(); loadLibs(); search();
</script>
</body>
</html>
"""
