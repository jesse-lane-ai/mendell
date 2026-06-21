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
import uuid
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


# In-flight import jobs, keyed by a generated id. An "Add folder" request can
# take minutes (especially with --recognize), so it runs in a background thread
# while the UI polls /api/add/progress for a real progress bar. Guarded by a
# lock since ThreadingHTTPServer handles each request on its own thread.
_IMPORT_JOBS: dict[str, dict] = {}
_IMPORT_JOBS_LOCK = threading.Lock()


def _job_set(job_id: str, **fields) -> None:
    with _IMPORT_JOBS_LOCK:
        _IMPORT_JOBS.setdefault(job_id, {}).update(fields)


def _job_get(job_id: str) -> dict | None:
    with _IMPORT_JOBS_LOCK:
        job = _IMPORT_JOBS.get(job_id)
        return dict(job) if job is not None else None


def _run_import_job(job_id: str, payload: dict) -> None:
    """Background worker for an 'Add folder' import. Updates the job's progress
    record as it goes and stashes the final result (or error) for the poller."""
    # recognize: a backend name, "" (filename-only), or "__default__"
    # (honor the library.recognizer config setting, like the CLI).
    recognize = payload.get("recognize", "__default__")
    if recognize == "__default__":
        recognize = config_mod.library_recognizer_default()
    elif recognize == "":
        recognize = None
    # For the ACE-Step captioner, let the UI pick the in-flight quantization
    # mode (full/8bit/4bit) by setting the env var the captioner reads.
    if recognize == "ace-step":
        load = (payload.get("captioner_load") or "").strip()
        if load:
            os.environ["ACESTEP_CAPTIONER_LOAD"] = load

    def on_progress(phase: str, done: int, total: int) -> None:
        _job_set(job_id, phase=phase, done=done, total=total)

    try:
        data = library_mod.add(
            payload["name"], payload["path"],
            tags=[t.strip() for t in (payload.get("tags") or "").split(",") if t.strip()] or None,
            analyze=bool(payload.get("analyze")),
            recognize=recognize,
            progress=on_progress,
        )
        _job_set(job_id, phase="done", result=data)
    except Exception as err:  # noqa: BLE001
        code = getattr(err, "code", 3)
        _job_set(job_id, phase="error", error=str(err), code=code)
    finally:
        # Release the captioner's VRAM once the scan is done (even on failure) —
        # the server is long-lived and would otherwise pin ~6–22 GB. Set
        # MENDELL_CAPTIONER_KEEP_WARM=1 to keep it resident for fast re-imports.
        if recognize == "ace-step" and not os.environ.get("MENDELL_CAPTIONER_KEEP_WARM"):
            try:
                from .ace.captioner import get_captioner
                get_captioner().unload()
            except Exception:
                pass


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
            elif path == "/api/fs/list":
                self._send_json({"ok": True, "data": self._fs_list(query)})
            elif path == "/api/add/progress":
                job_id = query.get("job", [None])[0]
                job = _job_get(job_id) if job_id else None
                if job is None:
                    raise MendellError("unknown import job", code=1)
                # Drop a finished job from the store after it's been read out, so
                # the dict doesn't grow without bound over a long server session.
                if job.get("phase") in ("done", "error"):
                    with _IMPORT_JOBS_LOCK:
                        _IMPORT_JOBS.pop(job_id, None)
                self._send_json({"ok": True, "data": job})
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
            elif path == "/api/clip/midi":
                proj_path = query.get("path", [None])[0]
                track = query.get("track", [None])[0]
                clip = query.get("clip", [None])[0]
                if not (proj_path and track and clip):
                    raise MendellError("path, track and clip are required", code=1)
                data = arrange_view_mod.clip_midi_content(
                    Path(proj_path).expanduser(), track, clip)
                self._send_json({"ok": True, "data": data})
            elif path == "/api/clip/audio-peaks":
                proj_path = query.get("path", [None])[0]
                track = query.get("track", [None])[0]
                clip = query.get("clip", [None])[0]
                if not (proj_path and track and clip):
                    raise MendellError("path, track and clip are required", code=1)
                data = arrange_view_mod.clip_audio_peaks(
                    Path(proj_path).expanduser(), track, clip)
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
            elif parsed.path == "/api/library/export":
                from . import library_bundle as bundle_mod
                out = payload["out"]
                if payload.get("mode") == "db":
                    data = bundle_mod.export_db(out)
                else:
                    inc = payload.get("include") or None
                    data = bundle_mod.export_bundle(out, include=inc)
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/library/import":
                from . import library_bundle as bundle_mod
                src = payload["src"]
                overwrite = bool(payload.get("overwrite"))
                if payload.get("mode") == "db":
                    data = bundle_mod.import_db(src, overwrite=overwrite)
                else:
                    dest = payload.get("dest") or str(Path(src).expanduser().parent / Path(src).stem)
                    data = bundle_mod.import_bundle(src, dest)
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
                # Imports can run for minutes, so kick the work onto a background
                # thread and hand the client a job id to poll for progress.
                job_id = uuid.uuid4().hex
                _job_set(job_id, phase="starting", done=0, total=0)
                threading.Thread(
                    target=_run_import_job, args=(job_id, dict(payload)), daemon=True
                ).start()
                self._send_json({"ok": True, "data": {"job": job_id}})
            elif parsed.path == "/api/library/rename":
                data = library_mod.rename(payload["name"], payload["new_name"])
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/library/tags":
                tags = payload.get("tags")
                if isinstance(tags, str):
                    tags = [t.strip() for t in tags.split(",") if t.strip()]
                data = library_mod.set_tags(payload["name"], tags or [])
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/library/rescan":
                data = library_mod.scan(payload["name"])
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/sample/update":
                fields = {k: payload[k] for k in ("category", "kind", "bpm") if payload.get(k) not in (None, "")}
                data = library_mod.update_file(payload["library"], payload["rel_path"], **fields)
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/sample/remove":
                data = library_mod.remove_file(payload["library"], payload["rel_path"])
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
            elif parsed.path == "/api/kits/randomize-all":
                data = kits_mod.randomize_all(
                    payload["kit"],
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
            elif parsed.path == "/api/arrange/move-clip":
                data = arrange_view_mod.move_clip(
                    Path(payload["path"]).expanduser(), payload["track"],
                    int(payload["from_bar"]), int(payload["to_bar"]),
                )
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/clip/audio-params":
                # Editor-phase save: persist audio-clip params via clips.set_params.
                fields = {k: payload[k] for k in
                          ("gain", "pitch", "loop", "loop_start", "loop_end",
                           "native_bpm", "warp")
                          if payload.get(k) is not None}
                data = arrange_view_mod.set_clip_audio_params(
                    Path(payload["path"]).expanduser(), payload["track"],
                    payload["clip"], **fields,
                )
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/clip/midi":
                # Editor-phase save: rewrite the clip's .mid from the note list.
                data = arrange_view_mod.save_clip_midi(
                    Path(payload["path"]).expanduser(), payload["track"],
                    payload["clip"], payload.get("notes") or [],
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
            elif parsed.path == "/api/arrange/add-track":
                data = arrange_view_mod.add_track(
                    Path(payload["path"]).expanduser(),
                    payload.get("name", ""), payload.get("type", ""),
                )
                self._send_json({"ok": True, "data": data})
            elif parsed.path == "/api/arrange/remove-track":
                data = arrange_view_mod.remove_track(
                    Path(payload["path"]).expanduser(), payload["name"],
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
    def _fs_list(self, q: dict) -> dict:
        """Browse the server's filesystem for the UI's folder/file pickers.

        ``path`` is the directory to list (defaults to the user's home);
        ``exts`` is an optional comma-separated allow-list of file extensions
        (in file-pick mode the UI passes e.g. ``.wav,.mp3``). Directories are
        always returned; files only when ``exts`` includes their suffix (or
        ``exts`` is empty, meaning any file)."""
        raw = (q.get("path", [None])[0] or "").strip()
        base = Path(raw).expanduser() if raw else Path.home()
        try:
            base = base.resolve()
        except OSError:
            base = Path.home()
        if not base.is_dir():
            base = base.parent if base.parent.is_dir() else Path.home()

        exts_raw = (q.get("exts", [None])[0] or "").strip().lower()
        exts = {e if e.startswith(".") else "." + e
                for e in (s.strip() for s in exts_raw.split(",")) if e}

        dirs: list[dict] = []
        files: list[dict] = []
        try:
            for entry in sorted(base.iterdir(), key=lambda p: p.name.lower()):
                if entry.name.startswith("."):
                    continue
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    continue
                if is_dir:
                    dirs.append({"name": entry.name, "path": str(entry)})
                elif not exts or entry.suffix.lower() in exts:
                    files.append({"name": entry.name, "path": str(entry)})
        except PermissionError:
            pass

        parent = str(base.parent) if base.parent != base else None
        return {"path": str(base), "parent": parent, "dirs": dirs, "files": files}

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
  .brow { padding: 6px 10px; cursor: pointer; border-bottom: 1px solid #2a2a2a; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .brow:hover { background: #2a2a2a; }
  .pbar { height: 10px; background: #2a2a2a; border-radius: 5px; overflow: hidden; }
  .pfill { height: 100%; width: 0%; background: #4a9eff; transition: width .2s ease; }
  .pfill.indet { width: 35% !important; animation: indet 1.1s infinite ease-in-out; }
  @keyframes indet { 0% { margin-left: -35%; } 100% { margin-left: 100%; } }
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
    <button onclick="exportDlg.showModal()">⬇ Export</button>
    <button onclick="importDlg.showModal()">⬆ Import</button>
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
    <button onclick="openAddTrackDlg()">+ Track</button>
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
      <button onclick="kitRandomizeAll()" title="Fill all 16 pads with random one-shots">🎲 Randomize all</button>
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
        <button class="primary" style="width:100%;margin-bottom:8px" onclick="openKitPicker()">🔎 Browse library…</button>
        <input id="kpSetPath" placeholder="or paste a library ref / path" style="width:100%;margin-bottom:6px">
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

<dialog id="addTrackDlg">
  <h3>Add track</h3>
  <label>Name</label>
  <input id="atName" placeholder="e.g. bass, vocals, fx">
  <label>Type</label>
  <select id="atType"><option value="midi">midi</option><option value="audio">audio</option></select>
  <div id="atStatus" class="muted" style="font-size:12px;margin-top:6px"></div>
  <div class="row">
    <button onclick="addTrackDlg.close()">Cancel</button>
    <button class="primary" onclick="doAddTrack()">Add</button>
  </div>
</dialog>

<dialog id="addDlg">
  <h3>Register a sample folder</h3>
  <label>Name</label><span style="display:flex;gap:4px"><input id="addName" placeholder="blank = random" style="flex:1"><button type="button" title="random name" onclick="slugInto('addName')">🎲</button></span>
  <label>Folder path</label><span style="display:flex;gap:4px"><input id="addPath" placeholder="/home/you/samples/drums" style="flex:1"><button type="button" title="browse" onclick="browseFor('addPath','dir')">📁</button></span>
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
  <div id="addProgress" style="display:none;margin-top:14px">
    <div id="addProgLabel" class="muted" style="font-size:12px;margin-bottom:4px">Starting…</div>
    <div class="pbar"><div id="addProgFill" class="pfill"></div></div>
  </div>
  <div class="row">
    <button id="addCancelBtn" onclick="addDlg.close()">Cancel</button>
    <button id="addBtn" class="primary" onclick="doAdd()">Add</button>
  </div>
</dialog>

<dialog id="exportDlg">
  <h3>Export library</h3>
  <label>What to export</label>
  <select id="expMode" style="width:100%" onchange="onExpMode()">
    <option value="db">Full DB backup — catalog only, no audio copied (.db)</option>
    <option value="bundle">Portable bundle — catalog + copies of the audio files (.zip)</option>
  </select>
  <small style="opacity:.7">DB backup is small and fast; restore it on a machine that shares the same sample paths. A bundle is self-contained and portable anywhere.</small>
  <label style="margin-top:12px">Output path (on this machine)</label>
  <span style="display:flex;gap:4px"><input id="expOut" placeholder="/home/you/mendell-library.db" style="flex:1"><button type="button" title="browse for a destination folder" onclick="browseFor('expOut','dir', '', document.getElementById('expMode').value==='db' ? '.db' : '.zip')">📁</button></span>
  <div id="expIncludeRow" style="display:none;margin-top:10px">
    <label>Bundle contents</label>
    <span style="display:flex;gap:14px;flex-wrap:wrap">
      <label style="display:flex;align-items:center;gap:5px;cursor:pointer"><input type="checkbox" class="expInc" value="samples" checked style="width:auto">samples</label>
      <label style="display:flex;align-items:center;gap:5px;cursor:pointer"><input type="checkbox" class="expInc" value="kits" checked style="width:auto">kits</label>
      <label style="display:flex;align-items:center;gap:5px;cursor:pointer"><input type="checkbox" class="expInc" value="projects" checked style="width:auto">projects</label>
      <label style="display:flex;align-items:center;gap:5px;cursor:pointer"><input type="checkbox" class="expInc" value="midi" checked style="width:auto">midi</label>
    </span>
  </div>
  <div class="row">
    <button onclick="exportDlg.close()">Cancel</button>
    <button class="primary" onclick="doExport()">Export</button>
  </div>
</dialog>

<dialog id="importDlg">
  <h3>Import library</h3>
  <label>What to import</label>
  <select id="impMode" style="width:100%" onchange="onImpMode()">
    <option value="db">Full DB backup (.db) — replaces the current catalog</option>
    <option value="bundle">Portable bundle (.zip) — extracts audio + registers it</option>
  </select>
  <label style="margin-top:12px">Source path (on this machine)</label>
  <span style="display:flex;gap:4px"><input id="impSrc" placeholder="/home/you/mendell-library.db" style="flex:1"><button type="button" title="browse" onclick="browseFor('impSrc','file', document.getElementById('impMode').value==='db' ? '.db' : '.zip')">📁</button></span>
  <div id="impDestRow" style="display:none;margin-top:10px">
    <label>Extract bundle into</label>
    <span style="display:flex;gap:4px"><input id="impDest" placeholder="blank = folder next to the .zip" style="flex:1"><button type="button" title="browse" onclick="browseFor('impDest','dir')">📁</button></span>
  </div>
  <label id="impOverwriteRow" style="display:flex;align-items:center;gap:8px;margin-top:12px;cursor:pointer">
    <input type="checkbox" id="impOverwrite" style="width:auto"> Overwrite the existing library DB
  </label>
  <div class="row">
    <button onclick="importDlg.close()">Cancel</button>
    <button class="primary" onclick="doImport()">Import</button>
  </div>
</dialog>

<dialog id="browseDlg" style="width:560px;max-width:90vw">
  <h3 id="browseTitle">Choose a folder</h3>
  <div style="display:flex;gap:6px;align-items:center;margin-bottom:8px">
    <button type="button" onclick="browseUp()" title="up one level">⬆</button>
    <input id="browseCwd" style="flex:1" onkeydown="if(event.key==='Enter')browseLoad(this.value)">
    <button type="button" onclick="browseLoad(document.getElementById('browseCwd').value)">Go</button>
  </div>
  <div id="browseList" style="max-height:50vh;overflow:auto;border:1px solid #333;border-radius:6px"></div>
  <div class="row">
    <button onclick="browseDlg.close()">Cancel</button>
    <button id="browsePick" class="primary" onclick="browseChoose()">Select this folder</button>
  </div>
</dialog>

<dialog id="libManageDlg">
  <h3>Manage library</h3>
  <input type="hidden" id="lmOrig">
  <label>Name</label><input id="lmName">
  <label>Tags (comma-separated)</label><input id="lmTags" placeholder="drums,lofi">
  <div style="margin-top:12px">
    <button type="button" onclick="lmRescan()">↻ Re-scan this library</button>
    <small id="lmRescanMsg" class="muted" style="margin-left:8px"></small>
  </div>
  <div class="row">
    <button onclick="libManageDlg.close()">Cancel</button>
    <button class="primary" onclick="lmSave()">Save</button>
  </div>
</dialog>

<dialog id="sampleEditDlg">
  <h3>Edit sample</h3>
  <div id="seRef" class="muted" style="font-size:12px;margin-bottom:10px;word-break:break-all"></div>
  <input type="hidden" id="seLib"><input type="hidden" id="seRel">
  <label>Category</label><input id="seCategory" placeholder="kick / snare / loop / …">
  <label>Kind</label>
  <select id="seKind" style="width:100%">
    <option value="">(unchanged)</option><option value="one-shot">one-shot</option>
    <option value="loop">loop</option><option value="unknown">unknown</option>
  </select>
  <label>BPM</label><input id="seBpm" type="number" step="0.01" placeholder="leave blank to keep">
  <div class="row">
    <button onclick="sampleEditDlg.close()">Cancel</button>
    <button class="primary" onclick="saveSampleEdit()">Save</button>
  </div>
</dialog>

<dialog id="kitPickDlg" style="width:640px;max-width:92vw">
  <h3 style="margin-top:0">Pick a sample <span id="kpkNote" class="muted" style="font-size:13px;font-weight:400"></span></h3>
  <div style="display:flex;gap:6px;align-items:center;margin-bottom:8px;flex-wrap:wrap">
    <input id="kpkQuery" placeholder="search filename…" style="flex:1;min-width:160px" onkeydown="if(event.key==='Enter')kitPickSearch()">
    <input id="kpkCat" placeholder="category" style="width:120px" onkeydown="if(event.key==='Enter')kitPickSearch()">
    <input id="kpkLib" placeholder="library (optional)" style="width:130px" onkeydown="if(event.key==='Enter')kitPickSearch()">
    <button class="primary" onclick="kitPickSearch()">Search</button>
  </div>
  <div id="kpkStatus" class="muted" style="font-size:12px;margin-bottom:6px"></div>
  <div id="kpkList" style="max-height:52vh;overflow:auto;border:1px solid #333;border-radius:6px"></div>
  <div class="row">
    <button onclick="kitPickClose()">Cancel</button>
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

<dialog id="midiEditor" style="width:auto;max-width:95vw">
  <h3 id="meTitle">Piano roll</h3>
  <div style="display:flex;gap:12px;align-items:center;margin-bottom:8px;font-size:12px">
    <span class="muted" id="meMeta"></span>
    <label style="display:flex;align-items:center;gap:4px;margin:0;color:#aab3c2">Snap
      <select id="meSnap" style="width:auto;margin:0">
        <option value="1">1/4 (beat)</option>
        <option value="0.5">1/8</option>
        <option value="0.25" selected>1/16</option>
        <option value="0.125">1/32</option>
      </select>
    </label>
    <span class="muted" style="font-size:11px">click empty grid = add · drag = move · drag right edge = resize · select + Delete = remove</span>
  </div>
  <div id="mePane" style="display:flex;border:1px solid #2a2e35;border-radius:6px;overflow:auto;max-height:60vh;max-width:90vw">
    <canvas id="meKeys" style="display:block;background:#15171b"></canvas>
    <canvas id="meGrid" style="display:block;background:#15171b;cursor:crosshair"></canvas>
  </div>
  <div class="row" style="margin-top:12px">
    <span id="meStatus" class="muted" style="flex:1;font-size:12px"></span>
    <button onclick="meClose()">Cancel</button>
    <button class="primary" onclick="meSave()">Save</button>
  </div>
</dialog>

<dialog id="audioEditor" style="width:760px;max-width:94vw">
  <h3 id="audioEdTitle">Audio clip</h3>
  <div id="audioEdMeta" class="muted" style="font-size:12px;margin-bottom:8px"></div>
  <div style="position:relative;background:#0e1014;border:1px solid #2a2e35;border-radius:6px">
    <canvas id="audioEdCanvas" width="720" height="160"
            style="display:block;width:100%;height:160px;cursor:ew-resize"></canvas>
  </div>
  <div style="display:flex;gap:18px;flex-wrap:wrap;margin-top:12px">
    <div>
      <label>Gain (dB)</label>
      <input id="audioEdGain" type="number" step="0.5" min="-60" max="24" style="width:90px">
    </div>
    <div>
      <label>Pitch (semitones)</label>
      <input id="audioEdPitch" type="number" step="1" min="-48" max="48" style="width:90px">
    </div>
    <div>
      <label>Warp mode</label>
      <select id="audioEdWarp" style="width:120px">
        <option value="off">off</option>
        <option value="beats">beats</option>
        <option value="melodic">melodic</option>
        <option value="harmonic">harmonic</option>
        <option value="vocal">vocal</option>
        <option value="complex">complex</option>
      </select>
    </div>
    <div>
      <label>Native BPM</label>
      <input id="audioEdBpm" type="number" step="0.01" min="0" placeholder="(unset)" style="width:100px">
    </div>
  </div>
  <div style="display:flex;gap:18px;flex-wrap:wrap;align-items:flex-end;margin-top:12px">
    <label style="display:flex;align-items:center;gap:8px;cursor:pointer;margin:0">
      <input type="checkbox" id="audioEdLoop" style="width:auto" onchange="audioEdDraw()"> Loop
    </label>
    <div>
      <label>Loop start (s)</label>
      <input id="audioEdLoopStart" type="number" step="0.01" min="0" style="width:100px" oninput="audioEdDraw()">
    </div>
    <div>
      <label>Loop end (s) <span class="muted">0 = clip end</span></label>
      <input id="audioEdLoopEnd" type="number" step="0.01" min="0" style="width:100px" oninput="audioEdDraw()">
    </div>
    <span class="muted" style="font-size:12px">Drag the green (start) / red (end) markers on the waveform.</span>
  </div>
  <div class="row">
    <span id="audioEdStatus" class="muted" style="margin-right:auto"></span>
    <button onclick="audioEditor.close()">Cancel</button>
    <button class="primary" onclick="audioEdSave()">Save</button>
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

let _libs = [];
async function loadLibs() {
  const { libraries } = await api("/api/libraries");
  _libs = libraries;
  const el = document.getElementById("libs");
  let html = `<div class="lib ${activeLib===""?"active":""}" onclick="selectLib('')">All libraries</div>`;
  for (const l of libraries) {
    const lname = l.name.replace(/'/g, "\\'");
    html += `<div class="lib ${activeLib===l.name?"active":""}" onclick="selectLib('${lname}')">
      <span>${esc(l.name)}<br><small>${l.file_count} files</small></span>
      <span style="display:flex;gap:6px">
        <span class="x" title="manage" onclick="event.stopPropagation();manageLib('${lname}')">⚙</span>
        <span class="x" title="unregister" onclick="event.stopPropagation();removeLib('${lname}')">✕</span>
      </span>
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

let lastMatches = [];
function render(matches) {
  lastMatches = matches;
  const el = document.getElementById("results");
  if (!matches.length) { el.innerHTML = '<div class="empty">No matching samples.</div>'; return; }
  let html = `<table>
    <colgroup>
      <col class="c-play"><col class="c-ref"><col class="c-cat"><col class="c-kind">
      <col class="c-bpm"><col class="c-dur"><col class="c-inst"><col class="c-cap"><col class="c-tags"><col class="c-act">
    </colgroup>
    <thead><tr><th></th><th>ref</th><th>category</th><th>kind</th><th class="num">bpm</th><th class="num">dur</th><th>instruments</th><th>caption</th><th>tags</th><th></th></tr></thead><tbody>`;
  for (const m of matches) {
    const ref = m.ref.replace(/'/g, "\\'");
    const cap = esc(m.caption||"");
    html += `<tr>
      <td><button class="play" onclick="play(this,'${ref}')">▶</button></td>
      <td class="ref">${esc(m.ref)}</td>
      <td>${esc(m.category||"")}</td>
      <td class="muted">${esc(m.kind||"")}</td>
      <td class="num">${m.bpm?Math.round(m.bpm):""}</td>
      <td class="num muted">${m.duration?m.duration.toFixed(1)+"s":""}</td>
      <td>${(m.instruments||[]).map(i=>`<span class="tag">${esc(i)}</span>`).join("")}</td>
      <td class="cap muted" title="${cap}">${cap}</td>
      <td>${(m.tags||[]).map(x=>`<span class="tag">${esc(x)}</span>`).join("")}</td>
      <td class="num"><button class="play" title="edit" onclick="editSample('${ref}')">✎</button>
        <button class="play" title="remove" onclick="removeSample('${ref}')">🗑</button></td>
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

// ---- Library management (rename / tags / single re-scan) ----------------
function manageLib(name) {
  const lib = _libs.find(l => l.name === name);
  document.getElementById("lmOrig").value = name;
  document.getElementById("lmName").value = name;
  document.getElementById("lmTags").value = (lib && lib.tags ? lib.tags : []).join(", ");
  document.getElementById("lmRescanMsg").textContent = "";
  libManageDlg.showModal();
}
async function lmSave() {
  try {
    const orig = document.getElementById("lmOrig").value;
    const newName = document.getElementById("lmName").value.trim();
    const tags = document.getElementById("lmTags").value;
    let cur = orig;
    if (newName && newName !== orig) {
      await api("/api/library/rename", { method:"POST", body: JSON.stringify({name: orig, new_name: newName}) });
      cur = newName;
      if (activeLib === orig) activeLib = newName;
    }
    await api("/api/library/tags", { method:"POST", body: JSON.stringify({name: cur, tags}) });
    libManageDlg.close();
    loadLibs(); search();
  } catch(e) { alert(e.message); }
}
async function lmRescan() {
  const msg = document.getElementById("lmRescanMsg");
  try {
    msg.textContent = "re-scanning…";
    const name = document.getElementById("lmOrig").value;
    const r = await api("/api/library/rescan", { method:"POST", body: JSON.stringify({name}) });
    const scanned = (r.scanned && r.scanned[0]) ? r.scanned[0] : null;
    msg.textContent = "done" + (scanned ? " — " + scanned.file_count + " files" : "");
    loadLibs(); search();
  } catch(e) { msg.textContent = ""; alert(e.message); }
}

// ---- Per-sample edit / remove -------------------------------------------
function editSample(ref) {
  const slash = ref.indexOf("/");
  const lib = ref.slice(0, slash), rel = ref.slice(slash + 1);
  const m = (lastMatches || []).find(x => x.ref === ref) || {};
  document.getElementById("seRef").textContent = ref;
  document.getElementById("seLib").value = lib;
  document.getElementById("seRel").value = rel;
  document.getElementById("seCategory").value = m.category || "";
  document.getElementById("seKind").value = "";
  document.getElementById("seBpm").value = m.bpm != null ? m.bpm : "";
  sampleEditDlg.showModal();
}
async function saveSampleEdit() {
  try {
    const body = {
      library: document.getElementById("seLib").value,
      rel_path: document.getElementById("seRel").value,
    };
    const cat = document.getElementById("seCategory").value.trim();
    const kind = document.getElementById("seKind").value;
    const bpm = document.getElementById("seBpm").value;
    if (cat) body.category = cat;
    if (kind) body.kind = kind;
    if (bpm !== "") body.bpm = bpm;
    await api("/api/sample/update", { method:"POST", body: JSON.stringify(body) });
    sampleEditDlg.close();
    search();
  } catch(e) { alert(e.message); }
}
async function removeSample(ref) {
  if (!confirm(`Remove '${ref}' from the catalog? (file on disk is left untouched)`)) return;
  try {
    const slash = ref.indexOf("/");
    await api("/api/sample/remove", { method:"POST", body: JSON.stringify({
      library: ref.slice(0, slash), rel_path: ref.slice(slash + 1),
    })});
    search(); loadLibs();
  } catch(e) { alert(e.message); }
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

const PHASE_LABEL = { starting:"Starting…", scanning:"Scanning folder…",
  indexing:"Indexing samples", recognizing:"Recognizing audio" };
function setAddBusy(busy) {
  document.getElementById("addBtn").disabled = busy;
  document.getElementById("addProgress").style.display = busy ? "block" : "none";
}
function setProg(phase, done, total) {
  const lbl = document.getElementById("addProgLabel");
  const fill = document.getElementById("addProgFill");
  const name = PHASE_LABEL[phase] || phase;
  if (total > 0) {
    const pct = Math.min(100, Math.round(100 * done / total));
    fill.classList.remove("indet");
    fill.style.width = pct + "%";
    lbl.textContent = name + " — " + done + "/" + total + " (" + pct + "%)";
  } else {
    fill.classList.add("indet");
    lbl.textContent = name;
  }
}
async function doAdd() {
  if (!addPath.value.trim()) { alert("Path is required."); addPath.focus(); return; }
  try {
    setAddBusy(true);
    setProg("starting", 0, 0);
    const { job } = await api("/api/add", { method:"POST", body: JSON.stringify({
      name: nameOrSlug("addName"), path: addPath.value.trim(), tags: addTags.value,
      recognize: document.getElementById("addRecognize").value,
      captioner_load: document.getElementById("addCaptionerLoad").value,
      analyze: document.getElementById("addAnalyze").checked,
    })});
    // Poll for progress until the background import finishes or errors.
    while (true) {
      await new Promise(r => setTimeout(r, 400));
      const s = await api("/api/add/progress?job=" + encodeURIComponent(job));
      if (s.phase === "done") break;
      if (s.phase === "error") throw new Error(s.error || "import failed");
      setProg(s.phase, s.done || 0, s.total || 0);
    }
    setAddBusy(false);
    addDlg.close(); addName.value=addPath.value=addTags.value="";
    loadLibs(); search();
  } catch(e) { setAddBusy(false); alert(e.message); }
}

// ---- Library export / import -------------------------------------------
function onExpMode() {
  const db = document.getElementById("expMode").value === "db";
  document.getElementById("expIncludeRow").style.display = db ? "none" : "block";
  document.getElementById("expOut").placeholder = db
    ? "/home/you/mendell-library.db" : "/home/you/mendell-library.zip";
}
function onImpMode() {
  const db = document.getElementById("impMode").value === "db";
  document.getElementById("impDestRow").style.display = db ? "none" : "block";
  document.getElementById("impOverwriteRow").style.display = db ? "flex" : "none";
  document.getElementById("impSrc").placeholder = db
    ? "/home/you/mendell-library.db" : "/home/you/mendell-library.zip";
}
async function doExport() {
  try {
    const out = document.getElementById("expOut").value.trim();
    if (!out) { alert("Output path is required."); document.getElementById("expOut").focus(); return; }
    const mode = document.getElementById("expMode").value;
    const body = { mode, out };
    if (mode === "bundle") {
      body.include = [...document.querySelectorAll(".expInc:checked")].map(c => c.value);
      if (!body.include.length) { alert("Pick at least one bundle content type."); return; }
    }
    const r = await api("/api/library/export", { method:"POST", body: JSON.stringify(body) });
    exportDlg.close();
    if (mode === "db") {
      alert("Exported DB backup to " + r.path + " (" + r.bytes + " bytes).");
    } else {
      const n = (r.samples||[]).length + (r.kits||[]).length + (r.projects||[]).length + (r.midi||[]).length;
      alert("Exported bundle to " + (r.path || out) + " (" + n + " items).");
    }
  } catch(e) { alert(e.message); }
}
async function doImport() {
  try {
    const src = document.getElementById("impSrc").value.trim();
    if (!src) { alert("Source path is required."); document.getElementById("impSrc").focus(); return; }
    const mode = document.getElementById("impMode").value;
    const body = { mode, src };
    if (mode === "db") body.overwrite = document.getElementById("impOverwrite").checked;
    else body.dest = document.getElementById("impDest").value.trim();
    try {
      await api("/api/library/import", { method:"POST", body: JSON.stringify(body) });
    } catch(e) {
      // A DB import replaces the whole catalog; rather than dead-end on the
      // "pass overwrite" guard, confirm and retry with overwrite set.
      if (mode === "db" && !body.overwrite && /already exists/.test(e.message)) {
        if (!confirm("This will replace your current library catalog with the imported one. Continue?")) return;
        body.overwrite = true;
        document.getElementById("impOverwrite").checked = true;
        await api("/api/library/import", { method:"POST", body: JSON.stringify(body) });
      } else { throw e; }
    }
    importDlg.close();
    loadLibs(); search();
    alert("Import complete.");
  } catch(e) { alert(e.message); }
}

// ---- Server-side file/folder browser ------------------------------------
// All paths in this UI are on the machine running the server, so the native
// browser file picker can't help — this modal lists the server filesystem via
// /api/fs/list and writes the chosen path back into the target input.
let _browseTarget = null, _browseMode = "dir", _browseExts = "", _browseCwd = "", _browseSuffix = "";
document.getElementById("browseList").addEventListener("click", (e) => {
  const row = e.target.closest(".brow");
  if (!row || !row.dataset.path) return;
  if (row.dataset.kind === "file") browsePickFile(row.dataset.path);
  else browseLoad(row.dataset.path);
});
// `saveSuffix` (e.g. ".db"/".zip") marks a "choose folder for a NEW file" use:
// picking a folder appends a default filename so we never turn the folder name
// itself into the file (the /home/you/Downloads.db bug).
function browseFor(inputId, mode, exts, saveSuffix) {
  _browseTarget = inputId; _browseMode = mode || "dir"; _browseExts = exts || "";
  _browseSuffix = saveSuffix || "";
  document.getElementById("browseTitle").textContent =
    _browseMode === "file" ? "Choose a file" : "Choose a folder";
  document.getElementById("browsePick").style.display =
    _browseMode === "file" ? "none" : "";
  browseDlg.showModal();
  const cur = document.getElementById(inputId).value.trim();
  browseLoad(cur || "");
}
async function browseLoad(path) {
  try {
    const q = new URLSearchParams();
    if (path) q.set("path", path);
    if (_browseMode === "file" && _browseExts) q.set("exts", _browseExts);
    const d = await api("/api/fs/list?" + q.toString());
    _browseCwd = d.path;
    document.getElementById("browseCwd").value = d.path;
    let h = "";
    for (const dir of d.dirs)
      h += '<div class="brow" data-kind="dir" data-path="' + esc(dir.path) +
           '">📁 ' + esc(dir.name) + '</div>';
    if (_browseMode === "file")
      for (const f of d.files)
        h += '<div class="brow" data-kind="file" data-path="' + esc(f.path) +
             '">🎵 ' + esc(f.name) + '</div>';
    const list = document.getElementById("browseList");
    list.innerHTML = h || '<div style="padding:10px;opacity:.6">empty</div>';
  } catch(e) { alert(e.message); }
}
function browseUp() {
  const cwd = document.getElementById("browseCwd").value;
  const parent = cwd.replace(/\/+$/,"").replace(/\/[^/]*$/, "") || "/";
  browseLoad(parent);
}
function browseChoose() {
  if (_browseTarget) {
    let v = _browseCwd;
    if (_browseSuffix) v = v.replace(/\/+$/,"") + "/mendell-library" + _browseSuffix;
    document.getElementById(_browseTarget).value = v;
  }
  browseDlg.close();
}
function browsePickFile(p) {
  if (_browseTarget) document.getElementById(_browseTarget).value = p;
  browseDlg.close();
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
// DAW-style timeline: rows = tracks; clip blocks are absolutely-positioned divs
// whose WIDTH = length_bars × ARR_PXBAR and LEFT = (start_bar-1) × ARR_PXBAR.
// Each block carries its own server-derived colour. Blocks are drag-and-drop:
// dragging snaps to the nearest bar and POSTs /api/arrange/move-clip (rejecting
// overlaps). Empty track space is click-to-drop (arrFillBlock, mapped to bar).
const ARR_PXBAR = 56;       // pixels per bar
const ARR_LABELW = 140;     // track-label gutter width
const ARR_ROWH = 40;        // track row height
// Per-block editor hook (double-click). MIDI clips open the piano-roll editor;
// audio clips are handled by a separate effort and left untouched here.
function openClipEditor(trackIndex, startBar) {
  selectClip(trackIndex, startBar);
  const t = _arrTrack(trackIndex); if (!t) return;
  const p = (t.placements || []).find(x => x.start_bar === startBar);
  if (!p) return;
  const ct = p.clip_type || t.type;
  if (ct === "midi") { openMidiEditor(t.name, p.clip); return; }   // piano-roll editor
  if (ct === "audio") { openAudioClipEditor(trackIndex, startBar); return; }  // waveform editor
}

// ---- Audio waveform editor ----------------------------------------------
// Self-contained dialog for clip_type === "audio" blocks. Loads peaks +
// current params from /api/clip/audio-peaks, lets the user tweak
// gain/pitch/warp/native_bpm/loop and drag two loop markers over the
// waveform, then POSTs only changed fields to /api/clip/audio-params.
let _audioEd = null;   // {track, clip, peaks, length, params, dragging}

async function openAudioClipEditor(trackIndex, startBar) {
  const t = _arrTrack(trackIndex); if (!t) return;
  const p = (t.placements || []).find(x => x.start_bar === startBar);
  if (!p) return;
  if ((p.clip_type || t.type) !== "audio") return;   // audio-only
  if (!p.clip) return;

  const dlg = document.getElementById("audioEditor");
  document.getElementById("audioEdTitle").textContent = "Audio clip · " + p.clip;
  document.getElementById("audioEdMeta").textContent = "loading…";
  _audioEd = { track: t.name, clip: p.clip, peaks: [], length: 0, params: {}, dragging: null };
  try { dlg.showModal(); } catch (e) {}

  let d;
  try {
    const q = new URLSearchParams({ path: _arrProjectPath, track: t.name, clip: p.clip });
    d = await api("/api/clip/audio-peaks?" + q.toString());
  } catch (e) {
    document.getElementById("audioEdMeta").textContent = "Error: " + e.message;
    return;
  }
  const params = d.params || {};
  _audioEd.peaks = d.peaks || [];
  _audioEd.length = Number(d.length_seconds) || 0;
  _audioEd.params = params;

  const len = _audioEd.length;
  document.getElementById("audioEdMeta").textContent =
    "length " + (len ? len.toFixed(3) + "s" : "unknown") + " · " +
    _audioEd.peaks.length + " peak columns";

  // Bind controls to current params (fall back to module defaults).
  document.getElementById("audioEdGain").value = (params.gain != null) ? params.gain : 0;
  document.getElementById("audioEdPitch").value = (params.pitch != null) ? params.pitch : 0;
  document.getElementById("audioEdWarp").value = params.warp || "off";
  document.getElementById("audioEdBpm").value = (params.native_bpm != null) ? params.native_bpm : "";
  document.getElementById("audioEdLoop").checked = !!params.loop;
  document.getElementById("audioEdLoopStart").value =
    (params.loop_start != null) ? params.loop_start : 0;
  document.getElementById("audioEdLoopEnd").value =
    (params.loop_end != null) ? params.loop_end : 0;   // 0 = clip end
  document.getElementById("audioEdStatus").textContent = "";
  audioEdDraw();
}

// Effective loop end in seconds (0/blank/out-of-range => clip end).
function _audioEdLoopEndSec() {
  const len = _audioEd ? _audioEd.length : 0;
  let v = parseFloat(document.getElementById("audioEdLoopEnd").value);
  if (!isFinite(v) || v <= 0 || (len && v > len)) return len;
  return v;
}
function _audioEdLoopStartSec() {
  const len = _audioEd ? _audioEd.length : 0;
  let v = parseFloat(document.getElementById("audioEdLoopStart").value);
  if (!isFinite(v) || v < 0) v = 0;
  if (len && v > len) v = len;
  return v;
}

function audioEdDraw() {
  if (!_audioEd) return;
  const cv = document.getElementById("audioEdCanvas");
  const ctx = cv.getContext("2d");
  const W = cv.width, H = cv.height, mid = H / 2;
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = "#0e1014"; ctx.fillRect(0, 0, W, H);
  // zero line
  ctx.strokeStyle = "#2a2e35"; ctx.beginPath(); ctx.moveTo(0, mid); ctx.lineTo(W, mid); ctx.stroke();
  // waveform — one vertical line per peak column
  const peaks = _audioEd.peaks, n = peaks.length;
  if (n) {
    ctx.strokeStyle = "#5b7cfa";
    for (let i = 0; i < n; i++) {
      const x = Math.floor(i / n * W) + 0.5;
      const lo = peaks[i][0], hi = peaks[i][1];
      const y1 = mid - hi * (mid - 2);
      const y2 = mid - lo * (mid - 2);
      ctx.beginPath(); ctx.moveTo(x, y1); ctx.lineTo(x, y2 === y1 ? y2 + 1 : y2); ctx.stroke();
    }
  }
  const len = _audioEd.length;
  if (document.getElementById("audioEdLoop").checked && len > 0) {
    const s = _audioEdLoopStartSec(), e = _audioEdLoopEndSec();
    const xs = s / len * W, xe = e / len * W;
    ctx.fillStyle = "rgba(255,255,255,0.07)";
    ctx.fillRect(xs, 0, Math.max(0, xe - xs), H);
    // start marker (green)
    ctx.strokeStyle = "#37d67a"; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(xs, 0); ctx.lineTo(xs, H); ctx.stroke();
    // end marker (red)
    ctx.strokeStyle = "#e0563f";
    ctx.beginPath(); ctx.moveTo(xe, 0); ctx.lineTo(xe, H); ctx.stroke();
    ctx.lineWidth = 1;
  }
}

// Drag the loop markers directly on the canvas.
(function audioEdWireCanvas() {
  const cv = document.getElementById("audioEdCanvas");
  if (!cv) return;
  function secFromEvent(ev) {
    const r = cv.getBoundingClientRect();
    const frac = Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width));
    return frac * (_audioEd ? _audioEd.length : 0);
  }
  cv.addEventListener("mousedown", (ev) => {
    if (!_audioEd || !_audioEd.length) return;
    if (!document.getElementById("audioEdLoop").checked) {
      document.getElementById("audioEdLoop").checked = true;
    }
    const sec = secFromEvent(ev);
    const s = _audioEdLoopStartSec(), e = _audioEdLoopEndSec();
    // grab whichever marker is closer
    _audioEd.dragging = (Math.abs(sec - s) <= Math.abs(sec - e)) ? "start" : "end";
    audioEdApplyDrag(sec); ev.preventDefault();
  });
  window.addEventListener("mousemove", (ev) => {
    if (_audioEd && _audioEd.dragging) audioEdApplyDrag(secFromEvent(ev));
  });
  window.addEventListener("mouseup", () => { if (_audioEd) _audioEd.dragging = null; });
})();

function audioEdApplyDrag(sec) {
  const len = _audioEd.length;
  sec = Math.min(len, Math.max(0, sec));
  if (_audioEd.dragging === "start") {
    const e = _audioEdLoopEndSec();
    if (sec > e) sec = e;
    document.getElementById("audioEdLoopStart").value = sec.toFixed(3);
  } else if (_audioEd.dragging === "end") {
    const s = _audioEdLoopStartSec();
    if (sec < s) sec = s;
    document.getElementById("audioEdLoopEnd").value = sec.toFixed(3);
  }
  audioEdDraw();
}

async function audioEdSave() {
  if (!_audioEd) return;
  const prev = _audioEd.params || {};
  const body = { path: _arrProjectPath, track: _audioEd.track, clip: _audioEd.clip };
  const changed = {};
  function maybeNum(id, key, prevVal) {
    const raw = document.getElementById(id).value;
    if (raw === "" || raw == null) return;
    const v = parseFloat(raw);
    if (!isFinite(v)) return;
    if (prevVal == null || Math.abs(v - Number(prevVal)) > 1e-9) changed[key] = v;
  }
  maybeNum("audioEdGain", "gain", prev.gain);
  maybeNum("audioEdPitch", "pitch", prev.pitch);
  // native_bpm: only send if non-empty and changed (>0 required by backend)
  const bpmRaw = document.getElementById("audioEdBpm").value;
  if (bpmRaw !== "") {
    const bpm = parseFloat(bpmRaw);
    if (isFinite(bpm) && bpm > 0 && (prev.native_bpm == null || Math.abs(bpm - Number(prev.native_bpm)) > 1e-9))
      changed.native_bpm = bpm;
  }
  const warp = document.getElementById("audioEdWarp").value;
  if (warp !== (prev.warp || "off")) changed.warp = warp;
  const loop = document.getElementById("audioEdLoop").checked;
  if (loop !== !!prev.loop) changed.loop = loop;
  const ls = _audioEdLoopStartSec();
  if (prev.loop_start == null || Math.abs(ls - Number(prev.loop_start)) > 1e-9) changed.loop_start = ls;
  const leRaw = parseFloat(document.getElementById("audioEdLoopEnd").value);
  const le = (isFinite(leRaw) && leRaw > 0) ? leRaw : 0;   // 0 = clip end sentinel
  if (prev.loop_end == null ? le !== 0 : Math.abs(le - Number(prev.loop_end)) > 1e-9) changed.loop_end = le;

  if (!Object.keys(changed).length) {
    document.getElementById("audioEditor").close();
    return;
  }
  Object.assign(body, changed);
  const status = document.getElementById("audioEdStatus");
  status.textContent = "Saving…";
  try {
    await api("/api/clip/audio-params", { method: "POST", body: JSON.stringify(body) });
    document.getElementById("audioEditor").close();
    await loadArrangeView();
  } catch (e) {
    status.textContent = "Error: " + e.message;
  }
}

// ===== MIDI piano-roll editor ============================================
// Pitch range C2..C6 (inclusive) drawn top=high. Geometry constants:
const ME_LOW = 36, ME_HIGH = 84;       // MIDI note numbers (C2..C6)
const ME_ROWH = 14;                    // px per semitone row
const ME_KEYW = 44;                    // left keyboard gutter width
const ME_PXBEAT = 40;                  // px per beat
const ME_NAMES = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"];
let _meState = null;  // {track, clip, bpm, bpb, lengthBars, beats, notes:[{pitch,start_beat,length_beats,velocity}], sel}
let _meDrag = null;   // {mode:'move'|'resize'|'new', idx, ...}

function meNoteName(p) { return ME_NAMES[((p % 12) + 12) % 12] + (Math.floor(p / 12) - 1); }
function meSnap() { return parseFloat(document.getElementById("meSnap").value) || 0.25; }
function meRows() { return ME_HIGH - ME_LOW + 1; }
function mePitchToY(p) { return (ME_HIGH - p) * ME_ROWH; }
function meYToPitch(y) { return ME_HIGH - Math.floor(y / ME_ROWH); }

async function openMidiEditor(track, clip) {
  try {
    const d = await api("/api/clip/midi?path=" + encodeURIComponent(_arrProjectPath) +
      "&track=" + encodeURIComponent(track) + "&clip=" + encodeURIComponent(clip));
    const bpb = d.beats_per_bar || 4;
    const lengthBars = Math.max(1, d.length_bars || 1);
    _meState = {
      track, clip, bpm: d.bpm, bpb, lengthBars,
      beats: lengthBars * bpb,
      notes: (d.notes || []).map(n => ({
        pitch: n.pitch, start_beat: n.start_beat,
        length_beats: n.length_beats || meSnap(), velocity: n.velocity || 100,
      })),
      sel: -1,
    };
    document.getElementById("meTitle").textContent = "Piano roll — " + clip;
    document.getElementById("meMeta").textContent =
      d.bpm + " BPM · " + lengthBars + " bar" + (lengthBars > 1 ? "s" : "") + " · " + bpb + "/bar";
    document.getElementById("meStatus").textContent = "";
    meSizeCanvases();
    meRender();
    document.getElementById("midiEditor").showModal();
  } catch (e) { _arrSelStatus(e.message, true); }
}
function meClose() { document.getElementById("midiEditor").close(); _meState = null; _meDrag = null; }

function meSizeCanvases() {
  const s = _meState;
  const h = meRows() * ME_ROWH, w = s.beats * ME_PXBEAT;
  const keys = document.getElementById("meKeys");
  keys.width = ME_KEYW; keys.height = h;
  const grid = document.getElementById("meGrid");
  grid.width = w; grid.height = h;
}
function meRender() {
  const s = _meState; if (!s) return;
  // --- keyboard gutter ---
  const kc = document.getElementById("meKeys").getContext("2d");
  const h = meRows() * ME_ROWH;
  kc.clearRect(0, 0, ME_KEYW, h);
  for (let p = ME_LOW; p <= ME_HIGH; p++) {
    const y = mePitchToY(p), black = ME_NAMES[((p % 12) + 12) % 12].includes("#");
    kc.fillStyle = black ? "#1b1e24" : "#2a2e35";
    kc.fillRect(0, y, ME_KEYW, ME_ROWH);
    kc.strokeStyle = "#15171b"; kc.strokeRect(0, y, ME_KEYW, ME_ROWH);
    if (p % 12 === 0) { kc.fillStyle = "#aab3c2"; kc.font = "9px sans-serif";
      kc.fillText(meNoteName(p), 4, y + ME_ROWH - 3); }
  }
  // --- grid ---
  const g = document.getElementById("meGrid").getContext("2d");
  const w = s.beats * ME_PXBEAT;
  g.clearRect(0, 0, w, h);
  // pitch row backgrounds (highlight C rows + black keys subtly)
  for (let p = ME_LOW; p <= ME_HIGH; p++) {
    const y = mePitchToY(p), black = ME_NAMES[((p % 12) + 12) % 12].includes("#");
    g.fillStyle = black ? "#181a20" : "#15171b";
    g.fillRect(0, y, w, ME_ROWH);
    g.strokeStyle = "#202028"; g.beginPath(); g.moveTo(0, y); g.lineTo(w, y); g.stroke();
  }
  // beat + bar gridlines
  for (let b = 0; b <= s.beats; b++) {
    const x = b * ME_PXBEAT, bar = (b % s.bpb === 0);
    g.strokeStyle = bar ? "#3a4150" : "#23262e";
    g.lineWidth = bar ? 1.5 : 1;
    g.beginPath(); g.moveTo(x, 0); g.lineTo(x, h); g.stroke();
  }
  g.lineWidth = 1;
  // notes
  s.notes.forEach((n, i) => {
    const x = n.start_beat * ME_PXBEAT, y = mePitchToY(n.pitch);
    const ww = Math.max(n.length_beats * ME_PXBEAT, 4);
    g.fillStyle = (i === s.sel) ? "#ffd166" : "#3b5bdb";
    g.fillRect(x, y + 1, ww, ME_ROWH - 2);
    g.strokeStyle = "#0d0f12"; g.strokeRect(x + 0.5, y + 1.5, ww - 1, ME_ROWH - 3);
    g.fillStyle = "rgba(255,255,255,.5)"; g.fillRect(x + ww - 3, y + 1, 3, ME_ROWH - 2); // resize handle
  });
}
// hit-test: returns {idx, edge:bool} or null
function meHit(bx, by) {
  const s = _meState;
  for (let i = s.notes.length - 1; i >= 0; i--) {
    const n = s.notes[i], x = n.start_beat * ME_PXBEAT, y = mePitchToY(n.pitch);
    const ww = Math.max(n.length_beats * ME_PXBEAT, 4);
    if (bx >= x && bx <= x + ww && by >= y && by <= y + ME_ROWH)
      return { idx: i, edge: bx >= x + ww - 5 };
  }
  return null;
}
function meEvtXY(ev) {
  const r = document.getElementById("meGrid").getBoundingClientRect();
  return { x: ev.clientX - r.left, y: ev.clientY - r.top };
}
function meSnapBeat(beat) {
  const sn = meSnap();
  return Math.max(0, Math.round(beat / sn) * sn);
}
function meGridDown(ev) {
  const s = _meState; if (!s) return;
  const { x, y } = meEvtXY(ev);
  const hit = meHit(x, y);
  if (hit) {
    s.sel = hit.idx;
    const n = s.notes[hit.idx];
    if (hit.edge) {
      _meDrag = { mode: "resize", idx: hit.idx };
    } else {
      _meDrag = { mode: "move", idx: hit.idx, offBeat: x / ME_PXBEAT - n.start_beat };
    }
    meRender();
    return;
  }
  // empty grid → add note
  const pitch = Math.max(ME_LOW, Math.min(ME_HIGH, meYToPitch(y)));
  const start = meSnapBeat(x / ME_PXBEAT);
  const len = meSnap();
  s.notes.push({ pitch, start_beat: start, length_beats: len, velocity: 100 });
  s.sel = s.notes.length - 1;
  _meDrag = { mode: "move", idx: s.sel, offBeat: 0 };
  meRender();
}
function meGridMove(ev) {
  const s = _meState; if (!s) return;
  const { x, y } = meEvtXY(ev);
  if (!_meDrag) {
    const hit = meHit(x, y);
    document.getElementById("meGrid").style.cursor =
      hit ? (hit.edge ? "ew-resize" : "move") : "crosshair";
    return;
  }
  const n = s.notes[_meDrag.idx]; if (!n) return;
  if (_meDrag.mode === "resize") {
    const end = meSnapBeat(x / ME_PXBEAT);
    n.length_beats = Math.max(meSnap(), end - n.start_beat);
  } else {
    n.start_beat = meSnapBeat(x / ME_PXBEAT - _meDrag.offBeat);
    n.pitch = Math.max(ME_LOW, Math.min(ME_HIGH, meYToPitch(y)));
  }
  meRender();
}
function meGridUp() { _meDrag = null; }
function meKeyDown(ev) {
  if (!document.getElementById("midiEditor").open) return;
  if ((ev.key === "Delete" || ev.key === "Backspace") && _meState && _meState.sel >= 0) {
    ev.preventDefault();
    _meState.notes.splice(_meState.sel, 1);
    _meState.sel = -1;
    meRender();
  }
}
async function meSave() {
  const s = _meState; if (!s) return;
  document.getElementById("meStatus").textContent = "Saving…";
  try {
    await api("/api/clip/midi", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: _arrProjectPath, track: s.track, clip: s.clip,
        notes: s.notes.map(n => ({ pitch: n.pitch, start_beat: n.start_beat,
          length_beats: n.length_beats, velocity: n.velocity })) }) });
    meClose();
    await loadArrangeView();
  } catch (e) { document.getElementById("meStatus").textContent = e.message; }
}
// wire grid canvas + global key handler once the DOM is ready
(function meInit() {
  const grid = document.getElementById("meGrid");
  if (!grid) return;
  grid.addEventListener("mousedown", meGridDown);
  window.addEventListener("mousemove", meGridMove);
  window.addEventListener("mouseup", meGridUp);
  window.addEventListener("keydown", meKeyDown);
})();
function renderArrangeGrid(snap) {
  document.getElementById("arrMeta").textContent =
    snap.bpm + " BPM · " + snap.time_sig + " · " + snap.total_bars +
    " bars · click empty space to drop a block, drag a block to move it";
  const totalBars = snap.total_bars || 32;
  const tracks = snap.tracks || [];
  const laneW = totalBars * ARR_PXBAR;
  let html = '<div style="min-width:' + (ARR_LABELW + laneW) + 'px;font-size:11px">';

  // Bar-number ruler
  html += '<div style="display:flex;align-items:flex-end;height:20px">' +
    '<div class="muted" style="width:' + ARR_LABELW + 'px;flex:0 0 ' + ARR_LABELW + 'px;padding:0 6px">Track</div>' +
    '<div style="position:relative;width:' + laneW + 'px;height:100%">';
  for (let b = 1; b <= totalBars; b++)
    html += '<div class="muted" style="position:absolute;left:' + ((b - 1) * ARR_PXBAR) +
      'px;bottom:0;font-size:9px;border-left:1px solid #2a2e35;padding-left:2px;height:8px">' + b + '</div>';
  html += '</div></div>';

  tracks.forEach((track, ti) => {
    const badge = ({midi:"M",audio:"A",sampler:"S"})[track.type] || "?";
    const trkSel = (_arrSel && _arrSel.track === track.name && _arrSel.bar == null) ? ";box-shadow:0 0 0 2px #fff inset" : "";
    html += '<div style="display:flex;align-items:stretch;margin-top:4px">';
    // Track label
    html += '<div class="arr-tracklabel" data-ti="' + ti + '" onclick="selectTrack(' + ti + ')" title="select track" ' +
      'style="cursor:pointer;width:' + ARR_LABELW + 'px;flex:0 0 ' + ARR_LABELW + 'px;height:' + ARR_ROWH +
      'px;padding:0 6px;background:#1d2026;border-radius:5px;display:flex;align-items:center;gap:5px;overflow:hidden' + trkSel + '">' +
      '<span style="background:#3b5bdb;color:#fff;border-radius:3px;padding:0 4px;font-weight:600">' + badge + '</span>' +
      '<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + esc(track.name) + '">' + esc(track.name) + '</span></div>';
    // Lane: bar gridlines + click-to-drop, plus absolutely-positioned blocks.
    html += '<div class="arr-lane" data-ti="' + ti + '" ' +
      'onclick="arrLaneClick(event,' + ti + ')" ' +
      'ondragover="arrDragOver(event,' + ti + ')" ondrop="arrDrop(event,' + ti + ')" ' +
      'style="position:relative;width:' + laneW + 'px;height:' + ARR_ROWH +
      'px;background:#15171b;border:1px solid #2a2e35;border-radius:6px;overflow:hidden">';
    for (let b = 2; b <= totalBars; b++)
      html += '<div style="position:absolute;left:' + ((b - 1) * ARR_PXBAR) +
        'px;top:0;bottom:0;border-left:1px solid #20242b"></div>';
    (track.placements || []).forEach(p => {
      const draggable = (track.type === "midi" || track.type === "audio");
      const left = (p.start_bar - 1) * ARR_PXBAR;
      const width = Math.max(p.length_bars * ARR_PXBAR - 2, 18);
      const sel = (_arrSel && _arrSel.track === track.name && _arrSel.bar === p.start_bar) ? ";box-shadow:0 0 0 2px #fff inset" : "";
      html += '<div class="arr-block" data-ti="' + ti + '" data-bar="' + p.start_bar + '" ' +
        (draggable ? 'draggable="true" ondragstart="arrDragStart(event,' + ti + ',' + p.start_bar + ')" ' : '') +
        'onclick="event.stopPropagation();selectClip(' + ti + ',' + p.start_bar + ')" ' +
        'ondblclick="event.stopPropagation();openClipEditor(' + ti + ',' + p.start_bar + ')" ' +
        'title="' + esc(p.clip) + ' (' + p.length_bars + ' bar' + (p.length_bars > 1 ? 's' : '') + ', ' + esc(p.clip_type || track.type) + ')" ' +
        'style="position:absolute;left:' + left + 'px;top:2px;width:' + width + 'px;height:' + (ARR_ROWH - 4) +
        'px;background:' + (p.color || '#3b5bdb') + ';border-radius:4px;cursor:grab;display:flex;align-items:center;' +
        'color:#fff;overflow:hidden' + sel + '">' +
        '<span style="font-size:9px;padding:0 4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(p.clip) + '</span></div>';
    });
    html += '</div></div>';
  });
  if (!tracks.length) html += '<div style="color:#8b93a1;padding:20px;text-align:center">No tracks yet — use the random fill buttons in the toolbar.</div>';
  html += "</div>";
  document.getElementById("arrGrid").innerHTML = html;
}

// Map an x-offset within a lane to a 1-indexed bar (snap-to-bar).
function arrBarFromEvent(ev, ti) {
  const lane = ev.currentTarget.closest(".arr-lane") || ev.currentTarget;
  const rect = lane.getBoundingClientRect();
  const x = ev.clientX - rect.left;
  return Math.max(1, Math.floor(x / ARR_PXBAR) + 1);
}
// Click empty lane space → drop a new block at that bar (skip if on a block).
function arrLaneClick(ev, ti) {
  if (ev.target.closest(".arr-block")) return;
  arrFillBlock(ti, arrBarFromEvent(ev, ti));
}
// ---- drag-and-drop block move -------------------------------------------
let _arrDrag = null;   // {ti, fromBar}
function arrDragStart(ev, ti, fromBar) {
  _arrDrag = { ti, fromBar };
  ev.dataTransfer.effectAllowed = "move";
  try { ev.dataTransfer.setData("text/plain", String(fromBar)); } catch (e) {}
}
// Does a [start, start+len-1] span overlap any *other* block on the track?
function arrWouldOverlap(track, fromBar, toBar, len) {
  const start = toBar, end = toBar + len - 1;
  return (track.placements || []).some(p => {
    if (p.start_bar === fromBar) return false;
    const oEnd = p.start_bar + p.length_bars - 1;
    return start <= oEnd && p.start_bar <= end;
  });
}
function arrDragOver(ev, ti) {
  if (!_arrDrag || _arrDrag.ti !== ti) return;   // same-track moves only
  const t = _arrTrack(ti); if (!t) return;
  const toBar = arrBarFromEvent(ev, ti);
  const moving = (t.placements || []).find(p => p.start_bar === _arrDrag.fromBar);
  const len = moving ? moving.length_bars : 1;
  if (!arrWouldOverlap(t, _arrDrag.fromBar, toBar, len)) {
    ev.preventDefault();
    ev.dataTransfer.dropEffect = "move";
  }
}
async function arrDrop(ev, ti) {
  if (!_arrDrag || _arrDrag.ti !== ti) { _arrDrag = null; return; }
  ev.preventDefault();
  const t = _arrTrack(ti);
  const drag = _arrDrag; _arrDrag = null;
  if (!t) return;
  const toBar = arrBarFromEvent(ev, ti);
  if (toBar === drag.fromBar) return;
  const moving = (t.placements || []).find(p => p.start_bar === drag.fromBar);
  const len = moving ? moving.length_bars : 1;
  if (arrWouldOverlap(t, drag.fromBar, toBar, len)) { _arrSelStatus("Move would overlap another block.", true); return; }
  _arrSelStatus("Moving block…");
  try {
    await api("/api/arrange/move-clip", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: _arrProjectPath, track: t.name, from_bar: drag.fromBar, to_bar: toBar }) });
    if (_arrSel && _arrSel.track === t.name && _arrSel.bar === drag.fromBar) _arrSel.bar = toBar;
    await loadArrangeView();
  } catch (e) { _arrSelStatus(e.message, true); }
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
  applyArrSelection(); renderArrSelection();
}
function selectClip(ti, bar) {
  const t = _arrTrack(ti); if (!t) return;
  const p = (t.placements || []).find(x => x.start_bar === bar);
  _arrSel = { track: t.name, type: t.type, bar, clip: p ? p.clip : null };
  applyArrSelection(); renderArrSelection();
}
// Update selection highlight on the EXISTING grid nodes — never rebuild the
// grid here. Rebuilding (renderArrangeGrid) on a single click destroys the
// clicked block before the browser can pair it into a dblclick, which would
// stop openClipEditor from ever firing.
function applyArrSelection() {
  document.querySelectorAll("#arrGrid .arr-block, #arrGrid .arr-tracklabel")
    .forEach(el => { el.style.boxShadow = ""; });
  if (!_arrSel || !_arrSnap) return;
  const ti = (_arrSnap.tracks || []).findIndex(t => t.name === _arrSel.track);
  if (ti < 0) return;
  const sel = "0 0 0 2px #fff inset";
  if (_arrSel.bar == null) {
    const lbl = document.querySelector('#arrGrid .arr-tracklabel[data-ti="' + ti + '"]');
    if (lbl) lbl.style.boxShadow = sel;
  } else {
    const blk = document.querySelector('#arrGrid .arr-block[data-ti="' + ti + '"][data-bar="' + _arrSel.bar + '"]');
    if (blk) blk.style.boxShadow = sel;
  }
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
  if (s.bar == null) {
    h += '<button onclick="arrRemoveTrackSel()">🗑 Remove track</button>';
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

// ---- Track management ---------------------------------------------------
function openAddTrackDlg() {
  if (!_arrProjectPath) { alert("Pick a project first."); return; }
  document.getElementById("atName").value = "";
  document.getElementById("atType").value = "midi";
  document.getElementById("atStatus").textContent = "";
  document.getElementById("addTrackDlg").showModal();
}
async function doAddTrack() {
  const name = document.getElementById("atName").value.trim();
  const type = document.getElementById("atType").value;
  const st = document.getElementById("atStatus");
  if (!name) { st.textContent = "Name is required."; return; }
  try {
    await api("/api/arrange/add-track", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: _arrProjectPath, name, type }) });
    document.getElementById("addTrackDlg").close();
    await loadArrangeView();
  } catch (e) { st.textContent = "Error: " + e.message; }
}
async function arrRemoveTrackSel() {
  const s = _arrSel; if (!s) return;
  if (!confirm('Remove track "' + s.track + '"? Its placements will be cleared (clips/samples on disk are untouched).')) return;
  _arrSelStatus("Removing track…");
  try {
    await api("/api/arrange/remove-track", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: _arrProjectPath, name: s.track }) });
    _arrSel = null;
    await loadArrangeView();
  } catch (e) { _arrSelStatus(e.message, true); }
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

async function kitRandomizeAll() {
  if (!_selectedKit) return;
  const st = document.getElementById("kitStatus");
  st.textContent = "Randomizing all 16 pads…";
  try {
    const r = await api("/api/kits/randomize-all", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kit: _selectedKit, library: document.getElementById("kitPadLib").value.trim() || null }) });
    await showKit(encodeURIComponent(_selectedKit));
    const d = r.data || {};
    let msg = "Filled " + (d.filled_count || 0) + " of 16 pads";
    if (d.failures && d.failures.length) msg += " (" + d.failures.length + " failed)";
    st.textContent = msg;
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

// ---- Visual sample picker (browse the library, audition, assign) ---------
let _kpkAudio = null;     // currently-auditioning preview
let _kpkBtn = null;       // its play button, for ▶/⏸ toggle

function openKitPicker() {
  if (_selPad == null) { alert("Select a pad first"); return; }
  document.getElementById("kpkNote").textContent =
    "→ note " + _selPad + " (" + (PAD_CAT[_selPad] || "") + ")";
  document.getElementById("kpkQuery").value = "";
  document.getElementById("kpkCat").value = PAD_CAT[_selPad] || "";
  document.getElementById("kpkLib").value = document.getElementById("kitPadLib").value.trim();
  document.getElementById("kpkList").innerHTML = "";
  document.getElementById("kitPickDlg").showModal();
  kitPickSearch();
}

function kitPickStopAudio() {
  if (_kpkAudio) { _kpkAudio.pause(); _kpkAudio = null; }
  if (_kpkBtn) { _kpkBtn.textContent = "▶"; _kpkBtn = null; }
}

function kitPickClose() {
  kitPickStopAudio();
  document.getElementById("kitPickDlg").close();
}

async function kitPickSearch() {
  const status = document.getElementById("kpkStatus");
  const p = new URLSearchParams();
  const q = document.getElementById("kpkQuery").value.trim();
  const cat = document.getElementById("kpkCat").value.trim();
  const lib = document.getElementById("kpkLib").value.trim();
  if (q) p.set("query", q);
  if (cat) p.set("category", cat);
  if (lib) p.set("library", lib);
  status.textContent = "Searching…";
  try {
    const data = await api("/api/search?" + p.toString());
    const matches = data.matches || [];
    status.textContent = matches.length + " sample" + (matches.length === 1 ? "" : "s");
    const list = document.getElementById("kpkList");
    if (!matches.length) { list.innerHTML = '<div class="muted" style="padding:10px">No matches.</div>'; return; }
    list.innerHTML = matches.map(m => {
      const ref = m.ref;
      const meta = [m.category, m.kind, (m.bpm ? m.bpm + " bpm" : "")].filter(Boolean).join(" · ");
      return '<div style="display:flex;align-items:center;gap:8px;padding:6px 8px;border-bottom:1px solid #222">' +
        '<button class="kpkPlay" data-ref="' + esc(ref) + '" onclick="kitPickAudition(this)" style="width:32px">▶</button>' +
        '<div style="flex:1;min-width:0">' +
        '<div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(ref) + '</div>' +
        '<div class="muted" style="font-size:11px">' + esc(meta) + '</div></div>' +
        '<button class="primary" onclick="kitPickSelect(\'' + esc(ref).replace(/'/g, "\\'") + '\')">Select</button>' +
        '</div>';
    }).join("");
  } catch (e) { status.textContent = e.message; }
}

function kitPickAudition(btn) {
  const ref = btn.getAttribute("data-ref");
  // toggle off if this row is already playing
  if (_kpkBtn === btn) { kitPickStopAudio(); return; }
  kitPickStopAudio();
  _kpkAudio = new Audio("/api/audio?ref=" + encodeURIComponent(ref) + "&t=" + Date.now());
  _kpkBtn = btn;
  btn.textContent = "⏸";
  _kpkAudio.onended = () => { if (_kpkBtn === btn) { btn.textContent = "▶"; _kpkBtn = null; _kpkAudio = null; } };
  _kpkAudio.play().catch(() => { btn.textContent = "▶"; _kpkBtn = null; _kpkAudio = null; });
}

async function kitPickSelect(ref) {
  const note = _selPad;
  try {
    await api("/api/kits/set-slot", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kit: _selectedKit, note, path: ref }) });
    kitPickClose();
    await showKit(encodeURIComponent(_selectedKit));
    kitPadPlay(note);
  } catch (e) { document.getElementById("kpkStatus").textContent = e.message; }
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
