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

from . import arrange_view as arrange_view_mod
from . import beat as beat_mod
from . import beat_random32
from . import config as config_mod
from . import kits as kits_mod
from . import library as library_mod
from . import midi_catalog as midi_catalog_mod
from . import midi_gen as midi_gen_mod
from . import registry as registry_mod
from .clips.name_classify import classify_from_names
from .errors import MendellError
from .recognize import list_backends


def _json_bytes(obj: object) -> bytes:
    return json.dumps(obj).encode("utf-8")


_EXPORT_EXTS = (".wav", ".mp3", ".flac", ".ogg")


def _project_export(project_path: str) -> Path | None:
    """Return the most recently rendered export file for a project, or None.

    Looks in ``<project>/export/`` (the default `mendell export` output dir) and
    picks the newest audio file — so the UI previews the latest render.
    """
    export_dir = Path(project_path).expanduser() / "export"
    if not export_dir.is_dir():
        return None
    candidates = [p for p in export_dir.iterdir()
                  if p.is_file() and p.suffix.lower() in _EXPORT_EXTS]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _projects_with_preview() -> dict:
    """`registry.list_projects()` plus a `has_export` flag per project."""
    data = registry_mod.list_projects()
    for p in data.get("projects", []):
        p["has_export"] = _project_export(p["path"]) is not None
    return data


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
            elif path == "/api/projects":
                self._send_json({"ok": True, "data": _projects_with_preview()})
            elif path == "/api/projects/audio":
                self._serve_project_audio(query)
            elif path == "/api/beat/options":
                self._send_json({"ok": True, "data": {
                    "styles": sorted(beat_mod.STYLES),
                    "drum_styles": list(midi_gen_mod.DRUM_STYLES),
                    "random32_patterns": beat_random32.available_patterns(),
                    "keys": list(beat_random32.KEYS),
                }})
            elif path == "/api/search":
                self._send_json({"ok": True, "data": self._do_search(query)})
            elif path == "/api/audio":
                self._serve_audio(query)
            elif path == "/api/kits":
                self._send_json({"ok": True, "data": kits_mod.list_kits()})
            elif path == "/api/kits/show":
                name = query.get("name", [None])[0]
                if not name:
                    raise MendellError("name is required", code=1)
                self._send_json({"ok": True, "data": kits_mod.show(name)})
            elif path == "/api/kits/audio":
                self._serve_kit_audio(query)
            elif path == "/api/arrange/view":
                proj_path = query.get("path", [None])[0]
                if not proj_path:
                    raise MendellError("path is required", code=1)
                data = arrange_view_mod.view(Path(proj_path).expanduser())
                self._send_json({"ok": True, "data": data})
            elif path == "/api/midilib/list":
                category = query.get("category", [None])[0] or None
                self._send_json({"ok": True, "data": midi_catalog_mod.list_clips(category=category)})
            elif path == "/api/midilib/show":
                name = query.get("name", [None])[0]
                if not name:
                    raise MendellError("name is required", code=1)
                self._send_json({"ok": True, "data": midi_catalog_mod.show(name)})
            elif path == "/api/midilib/summary":
                name = query.get("name", [None])[0]
                if not name:
                    raise MendellError("name is required", code=1)
                clip = midi_catalog_mod.show(name)
                self._send_json({"ok": True, "data": midi_catalog_mod.summary(clip["path"])})
            elif path == "/api/classify/probe":
                file_path_param = query.get("path", [None])[0]
                if not file_path_param:
                    raise MendellError("path is required", code=1)
                self._send_json({"ok": True, "data": {
                    "path": file_path_param,
                    "name_classify": classify_from_names(file_path_param),
                }})
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
            elif parsed.path == "/api/projects/sync":
                # Refresh a project's registry row from its project.toml on disk.
                data = registry_mod.record(payload["path"])
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/projects/remove":
                # Drop the registry entry only — files on disk are untouched.
                data = registry_mod.remove(payload["path"])
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/beat/new":
                self._send_json({"ok": True, "data": self._beat_new(payload)})
            elif parsed.path == "/api/beat/make":
                self._send_json({"ok": True, "data": self._beat_make(payload)})
            elif parsed.path == "/api/beat/random32":
                self._send_json({"ok": True, "data": self._beat_random32(payload)})
            elif parsed.path == "/api/projects/render":
                from . import engine as engine_mod
                project_dir = Path(payload["path"]).expanduser()
                data = engine_mod.export(project_dir, format=payload.get("format") or "wav")
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
                # captioner reads. We free the model after each import (below),
                # so a different mode picked next time actually takes effect.
                if recognize == "ace-step":
                    load = (payload.get("captioner_load") or "").strip()
                    if load:
                        os.environ["ACESTEP_CAPTIONER_LOAD"] = load
                try:
                    data = library_mod.add(
                        payload["name"], payload["path"],
                        tags=[t.strip() for t in (payload.get("tags") or "").split(",") if t.strip()] or None,
                        analyze=bool(payload.get("analyze")),
                        recognize=recognize,
                    )
                finally:
                    # Release the captioner's VRAM once the scan is done (even on
                    # failure) — the server is long-lived and would otherwise pin
                    # ~6–22 GB indefinitely. Set MENDELL_CAPTIONER_KEEP_WARM=1 to
                    # keep it resident for fast back-to-back imports instead.
                    if recognize == "ace-step" and not os.environ.get("MENDELL_CAPTIONER_KEEP_WARM"):
                        try:
                            from .ace.captioner import get_captioner
                            get_captioner().unload()
                        except Exception:
                            pass
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/kits/create":
                data = kits_mod.create(payload["name"], description=payload.get("description") or "")
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/kits/add":
                data = kits_mod.add_slot(
                    payload["kit"], payload["note_or_category"], payload["path"],
                    slot_name=payload.get("slot_name") or "",
                )
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/kits/quick":
                seed = payload.get("seed")
                data = kits_mod.quick_kit(
                    payload["name"],
                    library=payload.get("library") or None,
                    seed=int(seed) if seed not in (None, "") else None,
                )
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/kits/apply":
                data = kits_mod.apply_to_project(
                    payload["kit"], Path(payload["project"]).expanduser(), payload["track"],
                )
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/kits/remove":
                data = kits_mod.remove(payload["name"])
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/kits/random-slot":
                data = kits_mod.random_slot(
                    payload["kit"], payload["note"],
                    library=payload.get("library") or None,
                    seed=int(payload["seed"]) if payload.get("seed") not in (None, "") else None,
                )
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/kits/set-slot":
                data = kits_mod.add_slot(
                    payload["kit"], payload["note"], payload["path"],
                    slot_name=payload.get("slot_name") or "",
                )
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/kits/clear-slot":
                data = kits_mod.remove_slot(payload["kit"], payload["note"])
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/midilib/generate":
                data = midi_catalog_mod.generate(
                    payload["name"], style=payload["style"],
                    bars=int(payload.get("bars") or 1),
                    bpm=float(payload["bpm"]) if payload.get("bpm") else None,
                    category=payload.get("category") or "drums",
                )
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/midilib/create":
                data = midi_catalog_mod.create_from_notes(
                    payload["name"], payload["notes"],
                    bpm=float(payload.get("bpm") or 120),
                    bars=int(payload.get("bars") or 1),
                    category=payload.get("category") or "other",
                )
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/midilib/import":
                data = midi_catalog_mod.import_file(
                    payload["path"], name=payload["name"],
                    category=payload.get("category") or "other",
                )
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/midilib/remove":
                data = midi_catalog_mod.remove(payload["name"])
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/arrange/random-loop":
                data = arrange_view_mod.random_loop(
                    Path(payload["path"]).expanduser(), payload["track"],
                    bars=int(payload.get("bars") or 8),
                    library=payload.get("library") or None,
                    seed=payload.get("seed") or None,
                    start_bar=int(payload.get("start_bar") or 1),
                )
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/arrange/random-kit":
                data = arrange_view_mod.random_kit(
                    Path(payload["path"]).expanduser(),
                    name=payload.get("name") or None,
                    seed=payload.get("seed") or None,
                )
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/arrange/random-clip":
                data = arrange_view_mod.random_clip(
                    Path(payload["path"]).expanduser(), payload["track"],
                    style=payload.get("style") or "lofi",
                    bars=int(payload.get("bars") or 4),
                    seed=payload.get("seed") or None,
                    start_bar=int(payload.get("start_bar") or 1),
                )
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/arrange/randomize-clip":
                data = arrange_view_mod.randomize_clip(
                    Path(payload["path"]).expanduser(), payload["track"], int(payload["bar"]),
                    library=payload.get("library") or None,
                    seed=payload.get("seed") or None,
                )
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/arrange/replace-clip":
                data = arrange_view_mod.replace_clip(
                    Path(payload["path"]).expanduser(), payload["track"], int(payload["bar"]),
                    payload["source"],
                )
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/arrange/remove-clip":
                data = arrange_view_mod.remove_clip(
                    Path(payload["path"]).expanduser(), payload["track"], int(payload["bar"]),
                )
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/arrange/add-kit":
                data = arrange_view_mod.add_kit_to_track(
                    Path(payload["path"]).expanduser(), payload["track"],
                    kit=payload.get("kit") or None,
                    library=payload.get("library") or None,
                    seed=payload.get("seed") or None,
                )
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/arrange/new-project":
                data = self._beat_new_project(payload)
                self._send_json({"ok": True, "data": data})
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as err:  # noqa: BLE001
            self._error(err)

    # -- beat creation ----------------------------------------------------
    @staticmethod
    def _require_name(payload: dict) -> str:
        name = (payload.get("name") or "").strip()
        if not name:
            raise MendellError("project name is required", code=1)
        return name

    def _beat_new_project(self, payload: dict) -> dict:
        """Scaffold a bare project from the arrangement-view UI's New Project dialog."""
        from . import project as project_mod

        name = self._require_name(payload)
        parent, base = config_mod.resolve_project_parent(name)
        project_dir = project_mod.create(
            parent, base,
            bpm=float(payload.get("bpm") or 120),
            key=payload.get("key") or "C",
            scale=payload.get("scale") or "minor",
            time_sig=payload.get("time_sig") or "4/4",
        )
        registry_mod.record_safe(project_dir)
        return {"path": str(project_dir), "name": base}

    def _beat_new(self, payload: dict) -> dict:
        name = self._require_name(payload)
        parent, base = config_mod.resolve_project_parent(name)

        def opt(key):
            v = payload.get(key)
            return v if (v is not None and str(v).strip() != "") else None

        return beat_mod.new(
            parent, base,
            style=payload.get("style") or "lofi",
            library=opt("library"),
            bars=int(opt("bars")) if opt("bars") else 8,
            seed=int(opt("seed")) if opt("seed") else None,
            export=bool(payload.get("export")),
        )

    def _beat_make(self, payload: dict) -> dict:
        name = self._require_name(payload)
        parent, base = config_mod.resolve_project_parent(name)

        def opt(key):
            v = payload.get(key)
            return v if (v is not None and str(v).strip() != "") else None

        return beat_mod.make(
            parent, base,
            style=payload.get("style") or "lofi",
            bpm=float(opt("bpm")) if opt("bpm") else None,
            key=opt("key"),
            duration=opt("duration") or "60s",
            variations=int(opt("variations")) if opt("variations") else 8,
            kit=opt("kit"), melody=opt("melody"), bass=opt("bass"),
            export_format=opt("export") or "mp3",
        )

    def _beat_random32(self, payload: dict) -> dict:
        name = self._require_name(payload)
        parent, base = config_mod.resolve_project_parent(name)

        def opt(key):
            v = payload.get(key)
            return v if (v is not None and str(v).strip() != "") else None

        return beat_random32.render(
            parent, base, db_path=str(library_mod.db_path()),
            tempo=float(opt("bpm")) if opt("bpm") else None,
            key=opt("key"),
            seed=int(opt("seed")) if opt("seed") else None,
            export_format=opt("export") or "mp3",
            warp=None,
            pattern=opt("pattern") or "mutation-loop",
        )

    # -- endpoints --------------------------------------------------------
    def _do_search(self, q: dict) -> dict:
        def one(key):
            v = q.get(key, [None])[0]
            return v or None

        bpm = one("bpm")
        bpm_val = float(bpm) if bpm else None
        return library_mod.search(
            one("query"),
            library=one("library"),
            tag=one("tag"),
            category=one("category"),
            kind=one("kind"),
            instrument=one("instrument"),
            # Treat 0 (and below) as "no bpm filter" — the UI's number input can
            # emit "0", which would otherwise match nothing.
            bpm=bpm_val if bpm_val and bpm_val > 0 else None,
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
        self._stream_file(target)

    def _serve_kit_audio(self, q: dict):
        kit = q.get("kit", [None])[0]
        note = q.get("note", [None])[0]
        if not kit or note is None:
            self._send(400, b"missing kit/note", "text/plain")
            return
        try:
            target = kits_mod.slot_source_path(kit, note)
        except Exception:  # noqa: BLE001
            target = None
        if target is None or not target.is_file():
            self._send(404, b"slot has no audio", "text/plain")
            return
        self._stream_file(target)

    def _serve_project_audio(self, q: dict):
        path = q.get("path", [None])[0]
        if not path:
            self._send(400, b"missing path", "text/plain")
            return
        target = _project_export(path)
        if target is None:
            self._send(404, b"no rendered export - render the project first", "text/plain")
            return
        self._stream_file(target)

    def _stream_file(self, target: Path):
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
  body { margin: 0; font: 14px/1.4 system-ui, sans-serif; background: #14161a; color: #e6e8eb; overflow-x: hidden; }
  header { padding: 16px 20px; border-bottom: 1px solid #2a2e35; display: flex; align-items: center; gap: 16px; }
  h1 { font-size: 18px; margin: 0; font-weight: 600; }
  main { display: grid; grid-template-columns: 240px minmax(0, 1fr); min-height: calc(100vh - 57px); }
  aside { border-right: 1px solid #2a2e35; padding: 16px; }
  section { padding: 16px 20px; min-width: 0; }
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
  nav.tabs { display: flex; gap: 4px; }
  button.tab { background: transparent; border: none; border-bottom: 2px solid transparent; border-radius: 0; padding: 7px 12px; color: #8b93a1; }
  button.tab:hover { background: transparent; color: #e6e8eb; }
  button.tab.active { color: #e6e8eb; border-bottom-color: #3b5bdb; }
  header > span:last-of-type, #libActions, #projActions { margin-left: auto; }
  table { width: 100%; border-collapse: collapse; table-layout: fixed; }
  th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid #23262d; vertical-align: top; overflow-wrap: anywhere; }
  th { color: #8b93a1; font-weight: 500; position: sticky; top: 0; background: #14161a; }
  th.num, td.num { white-space: nowrap; }
  col.c-play { width: 44px; }
  col.c-ref { width: 22%; }
  col.c-cat { width: 9%; }
  col.c-kind { width: 7%; }
  col.c-bpm { width: 52px; }
  col.c-dur { width: 56px; }
  col.c-inst { width: 14%; }
  col.c-cap { width: 26%; }
  col.c-tags { width: 12%; }
  td.ref { word-break: break-all; }
  td.cap { font-size: 12px; line-height: 1.35; }
  .tag { display: inline-block; background: #232834; color: #aab3c2; border-radius: 4px; padding: 1px 6px; margin: 0 2px 2px 0; font-size: 12px; }
  .play { width: 30px; height: 30px; border-radius: 50%; padding: 0; }
  .play.on { background: #2f9e44; border-color: #2f9e44; }
  .muted { color: #8b93a1; }
  .count { color: #8b93a1; margin-left: auto; }
  .empty { color: #8b93a1; padding: 30px; text-align: center; }
  .card { background: #1b1e24; border: 1px solid #333842; border-radius: 8px; padding: 14px; min-width: 220px; flex: 1; max-width: 320px; }
  .card h3 { margin: 0 0 10px; font-size: 14px; }
  .card input, .card select { margin-bottom: 8px; }
  #kitList .kit-row, #midiClipList .midi-row { background: #1b1e24; border: 1px solid #333842; border-radius: 6px; padding: 8px 12px; margin-bottom: 8px; cursor: pointer; display: flex; align-items: center; gap: 10px; }
  #kitList .kit-row:hover, #midiClipList .midi-row:hover { background: #21252d; }
  dialog { background: #1b1e24; color: #e6e8eb; border: 1px solid #333842; border-radius: 10px; padding: 20px; width: 360px; }
  dialog h3 { margin: 0 0 12px; }
  dialog label { display: block; margin: 10px 0 4px; color: #aab3c2; }
  dialog input { width: 100%; }
  dialog .row { display: flex; gap: 8px; justify-content: flex-end; margin-top: 16px; }
</style>
</head>
<body>
<header>
  <h1>🎛 Mendell</h1>
  <nav class="tabs">
    <button id="tab-library" class="tab active" onclick="showTab('library')">Library</button>
    <button id="tab-projects" class="tab" onclick="showTab('projects')">Projects</button>
    <button id="tab-arrange" class="tab" onclick="showTab('arrange')">Arrangement</button>
    <button id="tab-kits" class="tab" onclick="showTab('kits')">Kits</button>
    <button id="tab-midi" class="tab" onclick="showTab('midi')">MIDI</button>
    <button id="tab-classify" class="tab" onclick="showTab('classify')">Classify</button>
  </nav>
  <span id="libActions">
    <button onclick="rescan()">Re-scan all</button>
    <button class="primary" onclick="addDlg.showModal()">+ Add folder</button>
  </span>
  <span id="projActions" style="display:none">
    <button onclick="loadProjects()">Refresh</button>
    <button class="primary" onclick="openBeatDlg()">+ New beat</button>
  </span>
  <span id="arrActions" style="display:none">
    <select id="arrProject" onchange="loadArrangeView()" style="min-width:200px">
      <option value="">— pick a project —</option>
    </select>
    <button onclick="newProjDlg.showModal()">+ New project</button>
    <button class="primary" onclick="randomFill('loop')">Random loop</button>
    <button class="primary" onclick="randomFill('kit')">Random kit</button>
    <button class="primary" onclick="randomFill('clip')">Random clip</button>
  </span>
  <span id="kitsActions" style="display:none">
    <button onclick="loadKits()">Refresh</button>
  </span>
  <span id="midiActions" style="display:none">
    <button onclick="midiLoadClips()">Refresh</button>
  </span>
  <span id="classifyActions" style="display:none"></span>
</header>
<main id="libraryView">
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

<section id="projectsView" style="display:none">
  <div id="projects"></div>
</section>

<section id="arrangeView" style="display:none; padding:16px 20px; width:100%">
  <div id="arrTransport" style="display:none;align-items:center;gap:10px;margin-bottom:12px;padding:8px 12px;background:#1d2026;border-radius:8px;flex-wrap:wrap">
    <button class="primary" onclick="arrRenderPlay()">▶ Render &amp; play</button>
    <button onclick="arrStop()">■ Stop</button>
    <audio id="arrAudio" preload="none" style="height:32px;vertical-align:middle"></audio>
    <span id="arrTransportStatus" class="muted" style="font-size:12px"></span>
  </div>
  <div id="arrMeta" class="muted" style="margin-bottom:12px"></div>
  <div id="arrGrid" style="overflow-x:auto"></div>
  <div id="arrSel" style="display:none;margin-top:14px"></div>
  <div id="arrEmpty" class="empty">Select a project above to view its arrangement.</div>
</section>

<section id="kitsView" style="display:none; padding:16px 20px; width:100%">
  <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:20px">
    <div class="card">
      <h3>Create kit</h3>
      <span style="display:flex;gap:4px"><input id="kitCreateName" placeholder="kit name (blank = random)" style="flex:1"><button type="button" title="random name" onclick="slugInto('kitCreateName')">🎲</button></span>
      <input id="kitCreateDesc" placeholder="description (optional)">
      <button class="primary" onclick="kitCreate()">Create</button>
    </div>
    <div class="card">
      <h3>Quick kit</h3>
      <span style="display:flex;gap:4px"><input id="kitQuickName" placeholder="kit name (blank = random)" style="flex:1"><button type="button" title="random name" onclick="slugInto('kitQuickName')">🎲</button></span>
      <input id="kitQuickLib" placeholder="library (optional)">
      <input id="kitQuickSeed" type="number" placeholder="seed (optional)">
      <button class="primary" onclick="kitQuick()">Quick kit (random one-shots)</button>
    </div>
  </div>
  <div id="kitStatus" class="muted" style="margin-bottom:8px"></div>
  <div id="kitList"></div>
  <div id="kitDetail" style="display:none;margin-top:20px">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
      <span id="kitDetailTitle" style="font-weight:600"></span>
      <label class="muted" style="margin-left:auto;font-size:12px">Random from
        <input id="kitPadLib" placeholder="any library" style="width:130px"></label>
      <button onclick="document.getElementById('kitDetail').style.display='none'">Close</button>
    </div>
    <div style="display:flex;gap:20px;flex-wrap:wrap;align-items:flex-start">
      <div>
        <div id="kitPads"></div>
        <div class="muted" style="font-size:11px;margin-top:6px">Click a pad to play · 🎲 random · ✕ clear</div>
      </div>
      <div id="kitPadPanel" class="card" style="min-width:240px;display:none">
        <div style="font-weight:600;margin-bottom:6px"><span id="kpNote"></span> · <span id="kpCat" class="muted"></span></div>
        <div id="kpFile" class="muted" style="font-size:11px;word-break:break-all;margin-bottom:8px">— empty —</div>
        <div style="display:flex;gap:6px;margin-bottom:8px">
          <button class="primary" onclick="kitPadPlay(_selPad)">▶ Play</button>
          <button onclick="kitPadRandom(_selPad)">🎲 Random</button>
          <button onclick="kitPadClear(_selPad)">✕ Clear</button>
        </div>
        <input id="kpSetPath" placeholder="library ref or /path/to/sample.wav" style="width:100%;margin-bottom:6px">
        <button onclick="kitPadSet(_selPad)">Set sample</button>
        <div id="kpStatus" class="muted" style="font-size:11px;margin-top:6px"></div>
      </div>
    </div>
    <details style="margin-top:14px"><summary class="muted" style="cursor:pointer;font-size:12px">All slots (table)</summary>
    <div id="kitDetailSlots" style="overflow-x:auto;margin-top:8px"></div></details>
    <div style="margin-top:14px;display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end">
      <input id="kitApplyProject" placeholder="/path/to/project" style="min-width:260px">
      <input id="kitApplyTrack" placeholder="track" value="drums" style="width:120px">
      <button class="primary" onclick="kitApply()">Apply to project</button>
      <button onclick="kitRemove()">Delete kit</button>
    </div>
    <div id="kitApplyStatus" class="muted" style="margin-top:8px"></div>
  </div>
</section>

<section id="midiView" style="display:none; padding:16px 20px; width:100%">
  <div style="display:flex;gap:12px;align-items:center;margin-bottom:10px">
    <select id="midiCatFilter" onchange="midiLoadClips()">
      <option value="">all categories</option>
      <option value="drums">drums</option><option value="bass">bass</option>
      <option value="melody">melody</option><option value="chords">chords</option>
      <option value="perc">perc</option><option value="other">other</option>
    </select>
  </div>
  <div id="midiClipList" style="margin-bottom:24px"></div>
  <div class="card" style="margin-bottom:20px;max-width:none">
    <h3>Generate from style preset</h3>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end">
      <input id="midiGenName" placeholder="pattern name (blank = random)" style="width:140px"><button type="button" title="random name" onclick="slugInto('midiGenName')">🎲</button>
      <select id="midiGenStyle"><option>boom-bap</option><option>lofi</option><option>trap</option></select>
      <input id="midiGenBars" type="number" value="1" min="1" max="32" style="width:70px" title="bars">
      <input id="midiGenBpm" type="number" placeholder="bpm" style="width:70px">
      <select id="midiGenCategory"><option>drums</option><option>bass</option><option>melody</option><option>chords</option><option>perc</option><option>other</option></select>
      <button class="primary" onclick="midiGenerate()">Generate</button>
      <span id="midiGenStatus" class="muted"></span>
    </div>
  </div>
  <div class="card" style="max-width:none">
    <h3>Step-grid note editor</h3>
    <p class="muted" style="font-size:12px;margin:4px 0 10px">Rows = pitches C3–B4, columns = 16 steps (1 bar in 4/4). Click cells to toggle; click a clip above to load it.</p>
    <div style="display:flex;gap:8px;align-items:flex-end;margin-bottom:8px;flex-wrap:wrap">
      <input id="midiEditorName" placeholder="save as" style="width:130px">
      <input id="midiEditorBpm" type="number" value="120" style="width:70px" title="bpm">
      <select id="midiEditorCategory"><option>other</option><option>drums</option><option>bass</option><option>melody</option><option>chords</option><option>perc</option></select>
      <button class="primary" onclick="midiEditorSave()">Save to catalog</button>
      <button onclick="midiEditorClear()">Clear</button>
      <span id="midiEditorStatus" class="muted"></span>
    </div>
    <div id="midiGridWrap" style="overflow-x:auto"></div>
  </div>
</section>

<section id="classifyView" style="display:none; padding:16px 20px; width:100%">
  <div class="card" style="max-width:560px">
    <h3>Classify a sample path</h3>
    <p class="muted" style="font-size:12px;margin:0 0 10px">Derives kind / category / key / bpm from the filename and parent-folder names.</p>
    <div style="display:flex;gap:8px">
      <input id="cpInput" placeholder="/path/to/sample.wav" style="flex:1" onkeydown="if(event.key==='Enter')classifyProbe()">
      <button class="primary" onclick="classifyProbe()">Probe</button>
    </div>
    <pre id="cpResult" style="background:#181a1f;border-radius:4px;padding:.8rem;margin:12px 0 0;min-height:3rem;white-space:pre-wrap"></pre>
  </div>
</section>

<dialog id="newProjDlg">
  <h3>New project</h3>
  <label>Name</label><span style="display:flex;gap:4px"><input id="npName" placeholder="blank = random" style="flex:1"><button type="button" title="random name" onclick="slugInto('npName')">🎲</button></span>
  <label>BPM</label><input id="npBpm" type="number" value="120" min="20" max="300">
  <label>Key</label>
  <select id="npKey"><option>C</option><option>C#</option><option>D</option><option>D#</option><option>E</option><option>F</option><option>F#</option><option>G</option><option>G#</option><option>A</option><option>A#</option><option>B</option></select>
  <label>Scale</label>
  <select id="npScale"><option>minor</option><option>major</option></select>
  <div class="row">
    <button onclick="newProjDlg.close()">Cancel</button>
    <button class="primary" onclick="createArrProject()">Create</button>
  </div>
</dialog>

<dialog id="addDlg">
  <h3>Register a sample folder</h3>
  <label>Name</label><span style="display:flex;gap:4px"><input id="addName" placeholder="blank = random" style="flex:1"><button type="button" title="random name" onclick="slugInto('addName')">🎲</button></span>
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

<dialog id="beatDlg" style="width:420px">
  <h3>Create a new beat</h3>
  <label>Mode</label>
  <select id="beatMode" style="width:100%" onchange="onBeatModeChange()">
    <option value="new">Quick scaffold — style preset, ready for kit load (beat new)</option>
    <option value="make">Full beat — variations + optional loops, auto-export (beat make)</option>
    <option value="random32">Random 32-bar — built from your sample library (beat random32)</option>
  </select>
  <p id="beatModeHelp" class="muted" style="font-size:12px;margin:8px 0 0"></p>

  <label>Project name</label><span style="display:flex;gap:4px"><input id="beatName" placeholder="blank = random" style="flex:1"><button type="button" title="random name" onclick="slugInto('beatName')">🎲</button></span>

  <div id="f-style"><label>Style</label>
    <select id="beatStyle" style="width:100%"></select></div>

  <!-- shared optional fields, shown per mode -->
  <div style="display:flex;gap:8px">
    <div id="f-bpm" style="flex:1"><label>BPM</label><input id="beatBpm" type="number" placeholder="auto"></div>
    <div id="f-key" style="flex:1"><label>Key</label><select id="beatKey" style="width:100%"></select></div>
  </div>

  <div id="f-make">
    <div style="display:flex;gap:8px">
      <div style="flex:1"><label>Duration</label><input id="beatDuration" placeholder="60s"></div>
      <div style="flex:1"><label>Variations</label><input id="beatVariations" type="number" placeholder="8"></div>
    </div>
    <label>Kit folder (one-shots, optional)</label><input id="beatKit" placeholder="/path/to/one-shots">
    <label>Melody loop (optional)</label><input id="beatMelody" placeholder="/path/to/melody.wav">
    <label>Bass loop (optional)</label><input id="beatBass" placeholder="/path/to/bass.wav">
  </div>

  <div id="f-random32">
    <label>Pattern archetype</label><select id="beatPattern" style="width:100%"></select>
    <label>Seed (optional, for reproducible picks)</label><input id="beatSeed" type="number" placeholder="random">
  </div>

  <div id="f-export"><label>Export format</label>
    <select id="beatExport" style="width:100%"><option>mp3</option><option>wav</option></select></div>

  <div class="row">
    <button onclick="beatDlg.close()">Cancel</button>
    <button class="primary" id="beatCreateBtn" onclick="doBeat()">Create</button>
  </div>
</dialog>

<audio id="player"></audio>
<script>
let activeLib = "";
let curBtn = null;
function esc(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
const player = document.getElementById("player");
player.onended = () => { if (curBtn) { curBtn.classList.remove("on"); curBtn.textContent="▶"; curBtn=null; } };

// Random word-slug generator for name fields (library/project/kit/pattern).
const SLUG_ADJ = ["amber","brisk","cosmic","dusty","electric","fuzzy","golden","hazy",
  "indigo","jade","lucid","mellow","neon","onyx","plush","quiet","rusty","silky",
  "tidal","umber","velvet","warm","wild","zesty","mystic","crisp","lush","bold"];
const SLUG_NOUN = ["otter","comet","grove","harbor","lotus","maple","nebula","onyx",
  "pulse","quartz","raven","summit","tundra","vapor","willow","zenith","ember","fjord",
  "glade","heron","koi","lagoon","monsoon","oasis","prairie","reef","saffron","thicket"];
function randomSlug() {
  const pick = a => a[Math.floor(Math.random() * a.length)];
  return pick(SLUG_ADJ) + "-" + pick(SLUG_NOUN) + "-" + Math.floor(Math.random() * 90 + 10);
}
// Fill an input with a fresh slug (used by the 🎲 buttons next to name fields).
function slugInto(id) { const el = document.getElementById(id); el.value = randomSlug(); el.focus(); }
// Return the trimmed value of a name input, generating + writing back a slug if blank.
function nameOrSlug(id) {
  const el = document.getElementById(id);
  let v = el.value.trim();
  if (!v) { v = randomSlug(); el.value = v; }
  return v;
}

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
  if (bpm && Number(bpm) > 0) p.set("bpm", bpm);
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
  let html = `<table>
    <colgroup>
      <col class="c-play"><col class="c-ref"><col class="c-cat"><col class="c-kind">
      <col class="c-bpm"><col class="c-dur"><col class="c-inst"><col class="c-cap"><col class="c-tags">
    </colgroup>
    <thead><tr><th></th><th>ref</th><th>category</th><th>kind</th><th class="num">bpm</th><th class="num">dur</th><th>instruments</th><th>caption</th><th>tags</th></tr></thead><tbody>`;
  for (const m of matches) {
    const ref = m.ref.replace(/'/g, "\\'");
    const cap = esc(m.caption||"");
    html += `<tr>
      <td><button class="play" onclick="play(this,'${ref}')">▶</button></td>
      <td class="ref">${m.ref}</td>
      <td>${m.category||""}</td>
      <td class="muted">${m.kind||""}</td>
      <td class="num">${m.bpm?Math.round(m.bpm):""}</td>
      <td class="num muted">${m.duration?m.duration.toFixed(1)+"s":""}</td>
      <td>${(m.instruments||[]).map(i=>`<span class="tag">${i}</span>`).join("")}</td>
      <td class="cap muted" title="${cap}">${cap}</td>
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
    if (!addPath.value.trim()) { alert("Path is required."); addPath.focus(); return; }
    await api("/api/add", { method:"POST", body: JSON.stringify({
      name: nameOrSlug("addName"), path: addPath.value.trim(), tags: addTags.value,
      recognize: document.getElementById("addRecognize").value,
      captioner_load: document.getElementById("addCaptionerLoad").value,
      analyze: document.getElementById("addAnalyze").checked,
    })});
    addDlg.close(); addName.value=addPath.value=addTags.value="";
    loadLibs(); search();
  } catch(e) { alert(e.message); }
}

// ---- Projects tab -------------------------------------------------------
let projectsLoaded = false;
// Tab registry — each entry maps a tab to its view + action-bar elements and an
// optional onShow hook. New feature tabs extend this object.
const TABS = {
  library:  { view: "libraryView",  actions: "libActions",      disp: "grid" },
  projects: { view: "projectsView", actions: "projActions",     disp: "block",
              onShow: () => { if (!projectsLoaded) loadProjects(); } },
  arrange:  { view: "arrangeView",  actions: "arrActions",      disp: "block",
              onShow: () => arrTabInit() },
  kits:     { view: "kitsView",     actions: "kitsActions",     disp: "block",
              onShow: () => loadKits() },
  midi:     { view: "midiView",     actions: "midiActions",     disp: "block",
              onShow: () => midiTabInit() },
  classify: { view: "classifyView", actions: "classifyActions", disp: "block" },
};
function showTab(name) {
  for (const [key, t] of Object.entries(TABS)) {
    document.getElementById(t.view).style.display = "none";
    const a = document.getElementById(t.actions);
    if (a) a.style.display = "none";
    const btn = document.getElementById("tab-" + key);
    if (btn) btn.classList.toggle("active", key === name);
  }
  const t = TABS[name] || TABS.library;
  document.getElementById(t.view).style.display = t.disp;
  const a = document.getElementById(t.actions);
  if (a) a.style.display = "";
  if (t.onShow) t.onShow();
}

function fmtDate(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString();
}

async function loadProjects() {
  projectsLoaded = true;
  const { projects } = await api("/api/projects");
  const el = document.getElementById("projects");
  if (!projects.length) {
    el.innerHTML = '<div class="empty">No projects registered yet. Create one with <code>mendell new</code> or <code>mendell beat new</code>.</div>';
    return;
  }
  let html = `<table>
    <thead><tr><th>name</th><th>genre</th><th>key</th><th class="num">bpm</th>
      <th class="num">sig</th><th>path</th><th>updated</th><th></th></tr></thead><tbody>`;
  for (const p of projects) {
    const path = esc(p.path).replace(/'/g, "\\'");
    html += `<tr>
      <td>${esc(p.name||"")}</td>
      <td class="muted">${esc(p.genre||"")}</td>
      <td>${esc([p.key, p.scale].filter(Boolean).join(" "))}</td>
      <td class="num">${p.bpm?Math.round(p.bpm):""}</td>
      <td class="num muted">${esc(p.time_sig||"")}</td>
      <td class="ref muted">${esc(p.path||"")}</td>
      <td class="muted">${fmtDate(p.last_updated)}</td>
      <td class="num">
        ${p.has_export
          ? `<button class="play" title="preview latest render" onclick="playProject(this,'${path}')">▶</button>`
          : ''}
        <button title="render &amp; preview" onclick="renderProject(this,'${path}')">⏺</button>
        <button title="refresh from project.toml" onclick="syncProject('${path}')">↻</button>
        <button title="remove from registry" onclick="removeProject('${path}','${esc(p.name||"").replace(/'/g,"\\'")}')">✕</button>
      </td>
    </tr>`;
  }
  el.innerHTML = html + "</tbody></table>";
}

function playProject(btn, path) {
  if (curBtn === btn) { player.pause(); btn.classList.remove("on"); btn.textContent="▶"; curBtn=null; return; }
  if (curBtn) { curBtn.classList.remove("on"); curBtn.textContent="▶"; }
  player.src = "/api/projects/audio?path=" + encodeURIComponent(path);
  player.play();
  btn.classList.add("on"); btn.textContent="⏸"; curBtn = btn;
}

async function renderProject(btn, path) {
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = "…";
  try {
    await api("/api/projects/render", { method:"POST", body: JSON.stringify({path}) });
    await loadProjects();
    // Auto-play the fresh render.
    player.src = "/api/projects/audio?path=" + encodeURIComponent(path) + "&t=" + Date.now();
    player.play();
  } catch(e) { alert(e.message); }
  finally { btn.disabled = false; btn.textContent = label; }
}

async function syncProject(path) {
  try { await api("/api/projects/sync", { method:"POST", body: JSON.stringify({path}) }); }
  catch(e) { alert(e.message); }
  loadProjects();
}

async function removeProject(path, name) {
  if (!confirm(`Remove '${name||path}' from the registry?\n(Files on disk are left untouched.)`)) return;
  try { await api("/api/projects/remove", { method:"POST", body: JSON.stringify({path}) }); }
  catch(e) { alert(e.message); }
  loadProjects();
}

// ---- New beat dialog ----------------------------------------------------
const BEAT_HELP = {
  new: "Fastest start: a project seeded with the style's tempo/key, a MIDI drum track routed to a sampler 'kit', and a looping starter pattern. Load samples with kit load afterward.",
  make: "Creates the project, generates humanized variations, optionally loads a kit and warps melody/bass loops, then renders and exports automatically. Can take a while.",
  random32: "Assembles a full 32-bar arrangement by picking drums/bass/melody from your registered sample library, then renders and exports. Needs samples in the library. Can take a while.",
};
const BEAT_FIELDS = {
  new:      ["f-style"],
  make:     ["f-style","f-bpm","f-key","f-make","f-export"],
  random32: ["f-bpm","f-key","f-random32","f-export"],
};
let beatOptsLoaded = false;
async function loadBeatOptions() {
  const o = await api("/api/beat/options");
  const fill = (id, items, blank) => {
    const sel = document.getElementById(id);
    sel.innerHTML = (blank ? `<option value="">${blank}</option>` : "") +
      items.map(x => `<option>${esc(x)}</option>`).join("");
  };
  fill("beatStyle", o.styles);
  fill("beatKey", o.keys, "auto");
  fill("beatPattern", o.random32_patterns);
  beatOptsLoaded = true;
}
function onBeatModeChange() {
  const mode = document.getElementById("beatMode").value;
  document.getElementById("beatModeHelp").textContent = BEAT_HELP[mode];
  const show = new Set(BEAT_FIELDS[mode]);
  for (const id of ["f-style","f-bpm","f-key","f-make","f-random32","f-export"])
    document.getElementById(id).style.display = show.has(id) ? "" : "none";
  document.getElementById("beatCreateBtn").textContent =
    mode === "new" ? "Create" : "Create & export";
}
async function openBeatDlg() {
  if (!beatOptsLoaded) await loadBeatOptions();
  onBeatModeChange();
  document.getElementById("beatDlg").showModal();
}
async function doBeat() {
  const mode = document.getElementById("beatMode").value;
  const v = id => document.getElementById(id).value.trim();
  const body = { name: nameOrSlug("beatName") };
  if (mode !== "random32") body.style = v("beatStyle");
  if (mode !== "new") {
    body.bpm = v("beatBpm"); body.key = v("beatKey"); body.export = v("beatExport");
  }
  if (mode === "make") {
    body.duration = v("beatDuration"); body.variations = v("beatVariations");
    body.kit = v("beatKit"); body.melody = v("beatMelody"); body.bass = v("beatBass");
  }
  if (mode === "random32") { body.pattern = v("beatPattern"); body.seed = v("beatSeed"); }
  const btn = document.getElementById("beatCreateBtn");
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = "Working…";
  try {
    await api("/api/beat/" + mode, { method:"POST", body: JSON.stringify(body) });
    document.getElementById("beatDlg").close();
    document.getElementById("beatName").value = "";
    loadProjects();
  } catch(e) { alert(e.message); }
  finally { btn.disabled = false; btn.textContent = label; }
}

loadBackends(); loadLibs(); search();
// ---- Arrangement tab ----------------------------------------------------
let _arrProjectPath = null;
let _arrSnap = null;          // last loaded arrangement snapshot
let _arrSel = null;           // {track, type, bar?, clip?}  current selection
async function arrTabInit() {
  try {
    const { projects } = await api("/api/projects");
    const sel = document.getElementById("arrProject");
    const cur = sel.value;
    sel.innerHTML = '<option value="">— pick a project —</option>';
    (projects || []).forEach(p => {
      const o = document.createElement("option");
      o.value = p.path;
      o.textContent = p.name + (p.bpm ? "  " + p.bpm + " BPM" : "");
      sel.appendChild(o);
    });
    if (cur) sel.value = cur;
  } catch (e) {}
  document.getElementById("arrEmpty").style.display = _arrProjectPath ? "none" : "";
}
async function loadArrangeView() {
  const path = document.getElementById("arrProject").value;
  if (!path) {
    _arrProjectPath = null; _arrSnap = null; _arrSel = null;
    document.getElementById("arrGrid").innerHTML = "";
    document.getElementById("arrMeta").textContent = "";
    document.getElementById("arrTransport").style.display = "none";
    document.getElementById("arrSel").style.display = "none";
    document.getElementById("arrEmpty").style.display = "";
    return;
  }
  _arrProjectPath = path;
  document.getElementById("arrEmpty").style.display = "none";
  document.getElementById("arrTransport").style.display = "flex";
  try {
    const snap = await api("/api/arrange/view?path=" + encodeURIComponent(path));
    _arrSnap = snap;
    renderArrangeGrid(snap);
    renderArrSelection();
  } catch (e) {
    document.getElementById("arrGrid").innerHTML = '<p style="color:#ff6b6b">Error: ' + esc(e.message) + '</p>';
  }
}
// Block sequencer: rows = tracks, columns = bar slots. Each square pad is a clip
// block — filled pads carry a clip (audio/midi), empty pads are click-to-add.
const ARR_COLOURS = ["#3b5bdb","#2f9e44","#c2410c","#7c3aed","#0e7490","#b45309"];
const ARR_PAD = 36;
function renderArrangeGrid(snap) {
  document.getElementById("arrMeta").textContent =
    snap.bpm + " BPM · " + snap.time_sig + " · " + snap.total_bars +
    " bars · click an empty pad to drop a clip block, a block to select it";
  const totalBars = snap.total_bars || 32;
  const tracks = snap.tracks || [];
  let html = '<div style="display:grid;grid-template-columns:140px repeat(' + totalBars +
    ',' + ARR_PAD + 'px);gap:3px;font-size:10px;align-items:center;min-width:' + (140 + totalBars * (ARR_PAD + 3)) + 'px">';
  html += '<div class="muted" style="padding:4px 6px">Track</div>';
  for (let b = 1; b <= totalBars; b++)
    html += '<div class="muted" style="text-align:center">' + b + '</div>';
  tracks.forEach((track, ti) => {
    const colour = ARR_COLOURS[ti % ARR_COLOURS.length];
    const barMap = {};
    (track.placements || []).forEach(p => {
      for (let b = p.start_bar; b < p.start_bar + p.length_bars && b <= totalBars; b++)
        barMap[b] = { clip: p.clip, start: p.start_bar, len: p.length_bars };
    });
    const badge = ({midi:"M",audio:"A",sampler:"S"})[track.type] || "?";
    const trkSel = (_arrSel && _arrSel.track === track.name && _arrSel.bar == null) ? ";box-shadow:0 0 0 2px #fff inset" : "";
    html += '<div onclick="selectTrack(' + ti + ')" title="select track" ' +
      'style="cursor:pointer;height:' + ARR_PAD + 'px;padding:0 6px;background:#1d2026;border-radius:5px;display:flex;align-items:center;gap:5px;overflow:hidden' + trkSel + '">' +
      '<span style="background:' + colour + ';color:#fff;border-radius:3px;padding:0 4px;font-weight:600">' + badge + '</span>' +
      '<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px" title="' + esc(track.name) + '">' + esc(track.name) + '</span></div>';
    for (let b = 1; b <= totalBars; b++) {
      const cell = barMap[b];
      if (cell) {
        const isStart = (b === cell.start);
        const isEnd = (b === cell.start + cell.len - 1) || (b === totalBars);
        const lr = isStart ? "6px" : "0", rr = isEnd ? "6px" : "0";
        const sel = (_arrSel && _arrSel.track === track.name && _arrSel.bar === cell.start) ? ";box-shadow:0 0 0 2px #fff inset" : "";
        html += '<div onclick="selectClip(' + ti + ',' + cell.start + ')" title="' + esc(cell.clip) + '" ' +
          'style="cursor:pointer;height:' + ARR_PAD + 'px;background:' + colour + ';border-radius:' + lr + ' ' + rr + ' ' + rr + ' ' + lr +
          ';display:flex;align-items:center;justify-content:center;color:#fff;overflow:hidden' + sel + '">' +
          (isStart ? '<span style="font-size:9px;padding:0 3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' +
            esc(cell.clip) + (cell.len > 1 ? ' &times;' + cell.len : '') + '</span>' : '') +
          '</div>';
      } else {
        html += '<div onclick="arrFillBlock(' + ti + ',' + b + ')" title="add a clip block at bar ' + b + '" ' +
          'style="cursor:pointer;height:' + ARR_PAD + 'px;background:#15171b;border:1px solid #2a2e35;border-radius:6px"' +
          ' onmouseover="this.style.background=\'#222733\'" onmouseout="this.style.background=\'#15171b\'"></div>';
      }
    }
  });
  if (!tracks.length) html += '<div style="grid-column:1/-1;color:#8b93a1;padding:20px;text-align:center">No tracks yet — use the random fill buttons in the toolbar.</div>';
  html += "</div>";
  document.getElementById("arrGrid").innerHTML = html;
}

// Click an empty pad → drop a clip block (random clip of the track's type).
async function arrFillBlock(ti, bar) {
  const t = _arrTrack(ti); if (!t) return;
  if (t.type !== "midi" && t.type !== "audio") { selectTrack(ti); return; }
  _arrSel = { track: t.name, type: t.type, bar, clip: null };
  renderArrSelection();
  _arrSelStatus("Adding block…");
  try {
    await api("/api/arrange/randomize-clip", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: _arrProjectPath, track: t.name, bar }) });
    await loadArrangeView();
  } catch (e) { _arrSelStatus(e.message, true); }
}

// ---- selection -----------------------------------------------------------
function _arrTrack(ti) { return (_arrSnap && _arrSnap.tracks) ? _arrSnap.tracks[ti] : null; }
function selectTrack(ti) {
  const t = _arrTrack(ti); if (!t) return;
  _arrSel = { track: t.name, type: t.type, bar: null, clip: null };
  renderArrangeGrid(_arrSnap); renderArrSelection();
}
function selectClip(ti, bar) {
  const t = _arrTrack(ti); if (!t) return;
  const p = (t.placements || []).find(x => x.start_bar === bar);
  _arrSel = { track: t.name, type: t.type, bar, clip: p ? p.clip : null };
  renderArrangeGrid(_arrSnap); renderArrSelection();
}

function renderArrSelection() {
  const el = document.getElementById("arrSel");
  if (!_arrSel) { el.style.display = "none"; return; }
  el.style.display = "";
  const s = _arrSel;
  let h = '<div class="card" style="max-width:560px">';
  h += '<div style="font-weight:600;margin-bottom:8px">' +
       (s.bar != null ? ('Clip @ bar ' + s.bar + (s.clip ? " · " + esc(s.clip) : " · (empty)")) : ('Track')) +
       ' <span class="muted">— ' + esc(s.track) + ' (' + esc(s.type) + ')</span></div>';
  h += '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px">';
  if (s.type === "midi" || s.type === "audio") {
    const barArg = (s.bar != null ? s.bar : 1);
    h += '<button class="primary" onclick="arrRandomizeSel(' + barArg + ')">🎲 Randomize ' + (s.clip ? 'clip' : 'into bar ' + barArg) + '</button>';
  }
  if (s.bar != null && s.clip) {
    h += '<button onclick="arrRemoveSel()">✕ Remove clip</button>';
  }
  if (s.type === "midi") {
    h += '<button onclick="arrAddKitSel()">🥁 Add kit</button>';
  }
  h += '</div>';
  // Replace-with-source row
  if (s.type === "audio") {
    h += '<div style="display:flex;gap:6px"><input id="arrReplaceSrc" placeholder="library ref or /path/to/sample.wav" style="flex:1">' +
         '<button onclick="arrReplaceSel()">Replace</button></div>';
  } else if (s.type === "midi") {
    h += '<div style="display:flex;gap:6px"><input id="arrReplaceSrc" placeholder="MIDI-catalog clip name" style="flex:1">' +
         '<button onclick="arrReplaceSel()">Replace</button></div>';
  }
  // Add-kit controls
  if (s.type === "midi") {
    h += '<div id="arrKitRow" style="display:flex;gap:6px;margin-top:8px;align-items:center">' +
         '<span class="muted" style="font-size:12px">Kit:</span>' +
         '<select id="arrKitName"><option value="">random from library</option></select>' +
         '<input id="arrKitLib" placeholder="library (optional)" style="width:140px"></div>';
  }
  h += '<div id="arrSelStatus" class="muted" style="font-size:12px;margin-top:8px"></div>';
  h += '</div>';
  el.innerHTML = h;
  if (s.type === "midi") populateArrKits();
}

async function populateArrKits() {
  try {
    const { kits } = await api("/api/kits");
    const sel = document.getElementById("arrKitName");
    if (!sel) return;
    sel.innerHTML = '<option value="">random from library</option>' +
      (kits || []).map(k => '<option value="' + esc(k.name) + '">' + esc(k.name) + '</option>').join("");
  } catch (e) {}
}

function _arrSelStatus(msg, err) {
  const st = document.getElementById("arrSelStatus");
  if (st) { st.style.color = err ? "#ff6b6b" : "#8b93a1"; st.textContent = msg; }
}

async function arrRandomizeSel(bar) {
  if (!_arrSel) return;
  _arrSelStatus("Randomizing…");
  try {
    await api("/api/arrange/randomize-clip", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: _arrProjectPath, track: _arrSel.track, bar,
        library: (document.getElementById("arrKitLib") || {}).value || null }) });
    _arrSel.bar = bar;
    await loadArrangeView();
  } catch (e) { _arrSelStatus(e.message, true); }
}

async function arrReplaceSel() {
  if (!_arrSel || _arrSel.bar == null) { _arrSelStatus("Select a clip/bar first.", true); return; }
  const src = (document.getElementById("arrReplaceSrc") || {}).value.trim();
  if (!src) { _arrSelStatus("Enter a source.", true); return; }
  _arrSelStatus("Replacing…");
  try {
    await api("/api/arrange/replace-clip", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: _arrProjectPath, track: _arrSel.track, bar: _arrSel.bar, source: src }) });
    await loadArrangeView();
  } catch (e) { _arrSelStatus(e.message, true); }
}

async function arrRemoveSel() {
  if (!_arrSel || _arrSel.bar == null) return;
  _arrSelStatus("Removing…");
  try {
    await api("/api/arrange/remove-clip", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: _arrProjectPath, track: _arrSel.track, bar: _arrSel.bar }) });
    _arrSel = { track: _arrSel.track, type: _arrSel.type, bar: null, clip: null };
    await loadArrangeView();
  } catch (e) { _arrSelStatus(e.message, true); }
}

async function arrAddKitSel() {
  if (!_arrSel) return;
  _arrSelStatus("Adding kit…");
  try {
    const data = await api("/api/arrange/add-kit", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: _arrProjectPath, track: _arrSel.track,
        kit: (document.getElementById("arrKitName") || {}).value || null,
        library: (document.getElementById("arrKitLib") || {}).value || null }) });
    await loadArrangeView();
    _arrSelStatus("Mapped " + data.mapped + " sounds onto '" + data.sampler_track + "'.");
  } catch (e) { _arrSelStatus(e.message, true); }
}

// ---- transport -----------------------------------------------------------
async function arrRenderPlay() {
  if (!_arrProjectPath) { alert("Pick a project first."); return; }
  const st = document.getElementById("arrTransportStatus");
  st.textContent = "Rendering…";
  try {
    await api("/api/projects/render", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: _arrProjectPath, format: "wav" }) });
    const au = document.getElementById("arrAudio");
    au.src = "/api/projects/audio?path=" + encodeURIComponent(_arrProjectPath) + "&t=" + Date.now();
    au.controls = true;
    await au.play().catch(() => {});
    st.textContent = "Playing rendered mix.";
  } catch (e) { st.textContent = "Render failed: " + e.message; }
}
function arrStop() {
  const au = document.getElementById("arrAudio");
  au.pause(); au.currentTime = 0;
  document.getElementById("arrTransportStatus").textContent = "Stopped.";
}
async function randomFill(kind) {
  if (!_arrProjectPath) { alert("Pick a project first."); return; }
  const payload = { path: _arrProjectPath };
  let endpoint;
  const sel = (_arrSel && _arrSel.track) ? _arrSel : null;
  if (kind === "kit") {
    endpoint = "/api/arrange/random-kit";
  } else {
    // 'loop' -> audio track, 'clip' -> midi track. Use the selected track when
    // its type matches; otherwise fall back to a type-specific default name so
    // an audio 'loops' track and a midi 'drums' track never collide.
    const want = (kind === "loop") ? "audio" : "midi";
    if (sel && sel.type !== want) {
      alert("'" + _arrSel.track + "' is a " + sel.type + " track. Random " + kind +
            " needs a " + want + " track — select a " + want + " track, or deselect to use the default.");
      return;
    }
    payload.track = sel ? sel.track : (kind === "loop" ? "loops" : "drums");
    if (kind === "loop") { payload.bars = 8; endpoint = "/api/arrange/random-loop"; }
    else { payload.style = "lofi"; payload.bars = 4; endpoint = "/api/arrange/random-clip"; }
  }
  try {
    await api(endpoint, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    await loadArrangeView();
  } catch (e) { alert("Random fill failed: " + e.message); }
}
async function createArrProject() {
  const name = nameOrSlug("npName");
  try {
    const data = await api("/api/arrange/new-project", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name,
        bpm: parseFloat(document.getElementById("npBpm").value) || 120,
        key: document.getElementById("npKey").value,
        scale: document.getElementById("npScale").value }) });
    document.getElementById("newProjDlg").close();
    await arrTabInit();
    document.getElementById("arrProject").value = data.path;
    await loadArrangeView();
  } catch (e) { alert("Failed to create project: " + e.message); }
}

// ---- Kits tab -----------------------------------------------------------
let _selectedKit = null;
async function loadKits() {
  const el = document.getElementById("kitList");
  try {
    const { kits } = await api("/api/kits");
    document.getElementById("kitStatus").textContent = kits.length + " kit" + (kits.length === 1 ? "" : "s");
    if (!kits.length) { el.innerHTML = '<div class="empty">No kits yet — use Create or Quick kit above.</div>'; return; }
    el.innerHTML = kits.map(k =>
      '<div class="kit-row" onclick="showKit(\'' + encodeURIComponent(k.name) + '\')">' +
      '<span style="font-weight:600">' + esc(k.name) + '</span>' +
      '<span class="muted">' + esc(k.description || "") + '</span></div>').join("");
  } catch (e) { document.getElementById("kitStatus").textContent = e.message; }
}
// 4x4 MPC-style pad grid over GM drum notes 36–51 (pad 1 = note 36, bottom-left).
const PAD_ROWS = [[48,49,50,51],[44,45,46,47],[40,41,42,43],[36,37,38,39]];
const PAD_CAT = {36:"kick",37:"rim",38:"snare",39:"clap",40:"perc",41:"perc",
  42:"hat",43:"perc",44:"perc",45:"tom",46:"openhat",47:"perc",48:"perc",
  49:"crash",50:"perc",51:"ride"};
let _kitSlots = {};   // gm_note -> slot
let _selPad = null;
let _padAudio = null;

async function showKit(nameEnc) {
  const name = decodeURIComponent(nameEnc);
  _selectedKit = name;
  try {
    const kit = await api("/api/kits/show?name=" + encodeURIComponent(name));
    document.getElementById("kitDetailTitle").textContent = kit.name + " (" + kit.slot_count + " slot" + (kit.slot_count === 1 ? "" : "s") + ")";
    _kitSlots = {};
    (kit.slots || []).forEach(s => { _kitSlots[s.gm_note] = s; });
    renderPads();
    const rows = (kit.slots || []).map(s =>
      '<tr><td style="padding:4px 10px 4px 0;color:#5b8cff">' + s.gm_note + '</td><td style="padding:4px 10px 4px 0">' +
      esc(s.note_name || "") + '</td><td style="padding:4px 10px 4px 0" class="muted">' + esc(s.category || "") +
      '</td><td style="padding:4px 0;word-break:break-all">' + esc(s.source_path || "") + '</td></tr>').join("");
    document.getElementById("kitDetailSlots").innerHTML =
      '<table style="border-collapse:collapse;width:100%;font-size:13px"><thead><tr class="muted" style="text-align:left">' +
      '<th style="padding:0 10px 4px 0">Note#</th><th style="padding:0 10px 4px 0">Name</th><th style="padding:0 10px 4px 0">Category</th><th>File</th></tr></thead><tbody>' + rows + '</tbody></table>';
    document.getElementById("kitDetail").style.display = "";
    document.getElementById("kitApplyStatus").textContent = "";
    if (_selPad != null) selectPad(_selPad);
  } catch (e) { alert(e.message); }
}

function renderPads() {
  const filled = "background:#2d3550;border-color:#5b8cff;color:#dfe6ff";
  const empty = "background:#15171b;border-color:#2a2e35;color:#5b6270";
  let html = '<div style="display:flex;flex-direction:column;gap:8px">';
  for (const row of PAD_ROWS) {
    html += '<div style="display:flex;gap:8px">';
    for (const note of row) {
      const s = _kitSlots[note];
      const cat = s ? (s.category || PAD_CAT[note]) : PAD_CAT[note];
      const label = s ? esc(s.name || cat) : esc(cat);
      const sel = (_selPad === note) ? "box-shadow:0 0 0 2px #fff inset;" : "";
      html += '<div onclick="padClick(' + note + ')" title="note ' + note + '" ' +
        'style="width:92px;height:78px;border:1px solid;border-radius:8px;cursor:pointer;' +
        'display:flex;flex-direction:column;justify-content:space-between;padding:6px;' + sel +
        (s ? filled : empty) + '">' +
        '<div style="font-size:10px;opacity:.7">' + note + ' · ' + esc(PAD_CAT[note]) + '</div>' +
        '<div style="font-size:11px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + label + '</div>' +
        '<div style="display:flex;gap:4px;justify-content:flex-end">' +
        '<span onclick="event.stopPropagation();kitPadRandom(' + note + ')" style="cursor:pointer">🎲</span>' +
        (s ? '<span onclick="event.stopPropagation();kitPadClear(' + note + ')" style="cursor:pointer">✕</span>' : '') +
        '</div></div>';
    }
    html += '</div>';
  }
  html += '</div>';
  document.getElementById("kitPads").innerHTML = html;
}

function padClick(note) { selectPad(note); kitPadPlay(note); }

function selectPad(note) {
  _selPad = note;
  renderPads();
  const s = _kitSlots[note];
  document.getElementById("kitPadPanel").style.display = "";
  document.getElementById("kpNote").textContent = "Note " + note;
  document.getElementById("kpCat").textContent = s ? (s.category || PAD_CAT[note]) : PAD_CAT[note];
  document.getElementById("kpFile").textContent = s ? (s.source_path || "") : "— empty —";
  document.getElementById("kpStatus").textContent = "";
}

function kitPadPlay(note) {
  if (note == null || !_kitSlots[note]) return;
  if (_padAudio) { _padAudio.pause(); }
  _padAudio = new Audio("/api/kits/audio?kit=" + encodeURIComponent(_selectedKit) + "&note=" + note + "&t=" + Date.now());
  _padAudio.play().catch(() => {});
}

async function kitPadRandom(note) {
  selectPad(note);
  const st = document.getElementById("kpStatus");
  st.textContent = "Picking…";
  try {
    await api("/api/kits/random-slot", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kit: _selectedKit, note, library: document.getElementById("kitPadLib").value.trim() || null }) });
    await showKit(encodeURIComponent(_selectedKit));
    kitPadPlay(note);
  } catch (e) { st.textContent = e.message; }
}

async function kitPadClear(note) {
  try {
    await api("/api/kits/clear-slot", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kit: _selectedKit, note }) });
    await showKit(encodeURIComponent(_selectedKit));
  } catch (e) { alert(e.message); }
}

async function kitPadSet(note) {
  const path = document.getElementById("kpSetPath").value.trim();
  if (!path) { alert("Enter a library ref or file path"); return; }
  try {
    await api("/api/kits/set-slot", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kit: _selectedKit, note, path }) });
    document.getElementById("kpSetPath").value = "";
    await showKit(encodeURIComponent(_selectedKit));
    kitPadPlay(note);
  } catch (e) { document.getElementById("kpStatus").textContent = e.message; }
}
async function kitCreate() {
  const name = nameOrSlug("kitCreateName");
  try {
    const data = await api("/api/kits/create", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, description: document.getElementById("kitCreateDesc").value.trim() }) });
    document.getElementById("kitCreateName").value = "";
    document.getElementById("kitCreateDesc").value = "";
    await loadKits(); showKit(encodeURIComponent(data.name));
  } catch (e) { alert(e.message); }
}
async function kitQuick() {
  const name = nameOrSlug("kitQuickName");
  document.getElementById("kitStatus").textContent = "Building quick kit…";
  try {
    const data = await api("/api/kits/quick", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, library: document.getElementById("kitQuickLib").value.trim() || null,
        seed: document.getElementById("kitQuickSeed").value || null }) });
    document.getElementById("kitQuickName").value = "";
    await loadKits(); showKit(encodeURIComponent(data.name));
  } catch (e) { alert(e.message); loadKits(); }
}
async function kitApply() {
  if (!_selectedKit) return;
  const project = document.getElementById("kitApplyProject").value.trim();
  const track = document.getElementById("kitApplyTrack").value.trim() || "drums";
  if (!project) { alert("Project path required"); return; }
  const el = document.getElementById("kitApplyStatus");
  el.textContent = "Applying…";
  try {
    const data = await api("/api/kits/apply", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kit: _selectedKit, project, track }) });
    el.style.color = "#2f9e44";
    el.textContent = "Mapped " + data.count + " slot" + (data.count === 1 ? "" : "s") + " onto '" + data.track + "'.";
  } catch (e) { el.style.color = "#ff6b6b"; el.textContent = e.message; }
}
async function kitRemove() {
  if (!_selectedKit || !confirm("Delete kit '" + _selectedKit + "'?")) return;
  try {
    await api("/api/kits/remove", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: _selectedKit }) });
    _selectedKit = null;
    document.getElementById("kitDetail").style.display = "none";
    loadKits();
  } catch (e) { alert(e.message); }
}

// ---- MIDI tab -----------------------------------------------------------
const MIDI_STEPS = 16;
const MIDI_PITCHES = Array.from({ length: 24 }, (_, i) => 71 - i);
const NOTE_NAMES = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"];
function midiNoteName(p) { return NOTE_NAMES[p % 12] + (Math.floor(p / 12) - 1); }
let midiGridState = new Set();
function midiInitGrid() {
  const wrap = document.getElementById("midiGridWrap");
  const table = document.createElement("table");
  table.style.cssText = "border-collapse:collapse;font-size:11px";
  MIDI_PITCHES.forEach(pitch => {
    const tr = document.createElement("tr");
    const lbl = document.createElement("td");
    lbl.textContent = midiNoteName(pitch);
    lbl.style.cssText = "padding:1px 6px 1px 0;text-align:right;color:#8b93a1;white-space:nowrap;width:32px";
    tr.appendChild(lbl);
    for (let s = 0; s < MIDI_STEPS; s++) {
      const td = document.createElement("td");
      td.id = "mg-" + pitch + "-" + s;
      td.style.cssText = "width:24px;height:20px;border:1px solid #2a2e35;cursor:pointer;background:#15171b";
      td.onclick = () => midiToggleCell(pitch, s);
      tr.appendChild(td);
    }
    table.appendChild(tr);
  });
  wrap.innerHTML = ""; wrap.appendChild(table);
}
function midiToggleCell(pitch, step) {
  const key = pitch + ":" + step;
  const el = document.getElementById("mg-" + pitch + "-" + step);
  if (midiGridState.has(key)) { midiGridState.delete(key); el.style.background = "#15171b"; }
  else { midiGridState.add(key); el.style.background = "#3b5bdb"; }
}
function midiEditorClear() {
  midiGridState.clear();
  document.querySelectorAll('[id^="mg-"]').forEach(el => el.style.background = "#15171b");
}
function midiGridLoadNotes(notes) {
  midiEditorClear();
  (notes || []).forEach(n => {
    const step = Math.round(n.start_beat / 0.25);
    if (step >= 0 && step < MIDI_STEPS && MIDI_PITCHES.includes(n.pitch)) {
      midiGridState.add(n.pitch + ":" + step);
      const el = document.getElementById("mg-" + n.pitch + "-" + step);
      if (el) el.style.background = "#3b5bdb";
    }
  });
}
async function midiEditorSave() {
  const name = document.getElementById("midiEditorName").value.trim();
  const bpm = parseFloat(document.getElementById("midiEditorBpm").value) || 120;
  const category = document.getElementById("midiEditorCategory").value;
  const st = document.getElementById("midiEditorStatus");
  if (!name) { st.textContent = "Enter a name first."; return; }
  if (!midiGridState.size) { st.textContent = "Grid is empty."; return; }
  const notes = [];
  midiGridState.forEach(key => {
    const [p, s] = key.split(":").map(Number);
    notes.push({ pitch: p, start_beat: s * 0.25, length_beats: 0.25, velocity: 100 });
  });
  st.textContent = "Saving…";
  try {
    await api("/api/midilib/create", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, notes, bpm, bars: 1, category }) });
    st.textContent = "Saved '" + name + "' (" + notes.length + " notes)."; midiLoadClips();
  } catch (e) { st.textContent = "Error: " + e.message; }
}
async function midiLoadClips() {
  const cat = document.getElementById("midiCatFilter").value;
  try {
    const { clips } = await api("/api/midilib/list" + (cat ? "?category=" + encodeURIComponent(cat) : ""));
    const el = document.getElementById("midiClipList");
    if (!clips.length) { el.innerHTML = '<div class="empty">No MIDI clips yet — generate or draw one below.</div>'; return; }
    el.innerHTML = clips.map(c =>
      '<div class="midi-row" onclick="midiLoadIntoEditor(\'' + encodeURIComponent(c.name) + '\')">' +
      '<span style="font-weight:600;min-width:140px">' + esc(c.name) + '</span>' +
      '<span class="muted">' + esc(c.category) + ' · ' + (c.bars ?? "-") + ' bars · ' + c.note_count + ' notes' + (c.bpm ? ' · ' + c.bpm + ' bpm' : "") + '</span>' +
      '<button style="margin-left:auto" onclick="event.stopPropagation();midiRemoveClip(\'' + encodeURIComponent(c.name) + '\')">✕</button></div>').join("");
  } catch (e) {}
}
async function midiLoadIntoEditor(nameEnc) {
  const name = decodeURIComponent(nameEnc);
  try {
    const data = await api("/api/midilib/summary?name=" + encodeURIComponent(name));
    document.getElementById("midiEditorName").value = name;
    if (data.bpm) document.getElementById("midiEditorBpm").value = data.bpm;
    midiGridLoadNotes(data.notes);
    document.getElementById("midiEditorStatus").textContent = "Loaded '" + name + "'.";
  } catch (e) { alert(e.message); }
}
async function midiRemoveClip(nameEnc) {
  const name = decodeURIComponent(nameEnc);
  if (!confirm("Remove '" + name + "' from catalog?")) return;
  try { await api("/api/midilib/remove", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }) }); midiLoadClips(); } catch (e) { alert(e.message); }
}
async function midiGenerate() {
  const name = nameOrSlug("midiGenName");
  const st = document.getElementById("midiGenStatus");
  const bpmRaw = document.getElementById("midiGenBpm").value;
  st.textContent = "Generating…";
  try {
    await api("/api/midilib/generate", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, style: document.getElementById("midiGenStyle").value,
        bars: parseInt(document.getElementById("midiGenBars").value) || 1,
        bpm: bpmRaw ? parseFloat(bpmRaw) : null,
        category: document.getElementById("midiGenCategory").value }) });
    st.textContent = "Generated '" + name + "'."; midiLoadClips();
  } catch (e) { st.textContent = "Error: " + e.message; }
}
function midiTabInit() {
  if (!document.getElementById("mg-71-0")) midiInitGrid();
  midiLoadClips();
}

// ---- Classify tab -------------------------------------------------------
async function classifyProbe() {
  const p = document.getElementById("cpInput").value.trim();
  const out = document.getElementById("cpResult");
  if (!p) { out.textContent = "← enter a file path"; return; }
  out.textContent = "probing…";
  try {
    const data = await api("/api/classify/probe?path=" + encodeURIComponent(p));
    const nc = data.name_classify;
    out.textContent = [
      "kind:        " + (nc.kind || "—"),
      "category:    " + (nc.category || "—"),
      "key:         " + (nc.key || "—") + "   scale: " + (nc.scale || "—"),
      "bpm:         " + (nc.bpm || "—"),
      "instruments: " + ((nc.instruments || []).join(", ") || "—"),
      "confidence:  " + (nc.confidence != null ? Number(nc.confidence).toFixed(3) : "—"),
      "source:      " + (nc.source || "—"),
    ].join("\n");
  } catch (e) { out.textContent = "Error: " + e.message; }
}
</script>
</body>
</html>
"""
