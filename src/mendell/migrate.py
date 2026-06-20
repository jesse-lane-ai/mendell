"""One-time project migration to the on-track instrument model.

Legacy projects used a separate ``sampler`` track type plus MIDI→sampler
``routes``. The current model hosts the sampler instrument directly on the MIDI
track. :func:`migrate_project` folds the old shape into the new one and is
idempotent — it no-ops once a project has no sampler tracks or routes left.

Folding rules:
- A MIDI track routed to a sampler track adopts that sampler as its instrument
  (the sampler TOML is moved onto the MIDI track's name); the MIDI track also
  inherits the sampler track's mixer/FX if it has none of its own.
- Several MIDI tracks routed to one sampler each get a copy.
- A MIDI track routed to several samplers (layering) hosts the first; the rest
  fall through to the orphan rule.
- An orphan sampler track (nothing routed to it) becomes its own MIDI track
  hosting the sampler, so no sounds are lost.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import paths
from .toml_io import read_toml, write_toml


def _inferred_mode(sdata: dict[str, Any]) -> str:
    """One full-range slot ⇒ "instrument"; otherwise "kit"."""
    slots = sdata.get("slots", [])
    if len(slots) == 1:
        s = slots[0]
        if s.get("note_low", 0) <= 0 and s.get("note_high", 0) >= 127:
            return "instrument"
    return "kit"


def _ensure_mode(project_dir: Path, track: str) -> None:
    path = paths.sampler_toml(project_dir, track)
    if not path.is_file():
        return
    sdata = read_toml(path)
    sampler = sdata.setdefault("sampler", {})
    if "mode" not in sampler:
        sampler["mode"] = _inferred_mode(sdata)
        write_toml(path, sdata)


def _adopt_sampler(project_dir: Path, src: str, dst: str) -> None:
    """Copy ``samplers/<src>.toml`` onto ``samplers/<dst>.toml`` (don't clobber)."""
    src_path = paths.sampler_toml(project_dir, src)
    dst_path = paths.sampler_toml(project_dir, dst)
    if not src_path.is_file() or dst_path.is_file():
        return
    sdata = read_toml(src_path)
    sdata.setdefault("sampler", {}).setdefault("mode", _inferred_mode(sdata))
    write_toml(dst_path, sdata)


def _remove_sampler_track(project_dir: Path, name: str) -> None:
    paths.track_toml(project_dir, name).unlink(missing_ok=True)
    paths.sampler_toml(project_dir, name).unlink(missing_ok=True)


def needs_migration(project_dir: Path) -> bool:
    tdir = paths.tracks_dir(project_dir)
    if not tdir.is_dir():
        return False
    for p in tdir.glob("*.toml"):
        data = read_toml(p)
        if data.get("track", {}).get("type") == "sampler" or data.get("routes"):
            return True
    return False


def migrate_project(project_dir: Path) -> dict[str, Any]:
    """Fold legacy sampler-track + routes projects into the on-track model.

    Idempotent. Returns ``{"migrated": bool, "folded": [...]}``.
    """
    project_dir = Path(project_dir)
    tdir = paths.tracks_dir(project_dir)
    if not tdir.is_dir():
        return {"migrated": False, "folded": []}

    raw = {p.stem: read_toml(p) for p in sorted(tdir.glob("*.toml"))}
    sampler_tracks = {n for n, d in raw.items() if d.get("track", {}).get("type") == "sampler"}
    if not sampler_tracks and not any(d.get("routes") for d in raw.values()):
        return {"migrated": False, "folded": []}

    folded: list[dict[str, Any]] = []
    consumed: set[str] = set()

    # MIDI tracks with routes adopt their (first) routed sampler as instrument.
    for midi, d in raw.items():
        if d.get("track", {}).get("type") != "midi":
            continue
        host_sampler: str | None = None
        for dest in d.get("routes", []):
            if dest not in sampler_tracks:
                continue
            if host_sampler is None:
                _adopt_sampler(project_dir, src=dest, dst=midi)
                host_sampler = dest
                consumed.add(dest)
            # extra layered samplers fall through to the orphan rule below
        if host_sampler is not None:
            d.setdefault("track", {})["instrument"] = {"type": "sampler"}
            sd = raw.get(host_sampler, {})
            for key in ("mixer", "fx", "fx_next_id", "automation"):
                if sd.get(key) and not d.get(key):
                    d[key] = sd[key]
            folded.append({"host": midi, "from": host_sampler})
        d.pop("routes", None)
        write_toml(paths.track_toml(project_dir, midi), d)
        _ensure_mode(project_dir, midi)

    for name in consumed:
        _remove_sampler_track(project_dir, name)

    # Orphan sampler tracks (nothing routed to them) become their own MIDI host.
    for name in sampler_tracks - consumed:
        sd = raw[name]
        sd.setdefault("track", {})["type"] = "midi"
        sd["track"]["instrument"] = {"type": "sampler"}
        sd.pop("routes", None)
        write_toml(paths.track_toml(project_dir, name), sd)
        _ensure_mode(project_dir, name)
        folded.append({"host": name, "from": name})

    return {"migrated": True, "folded": folded}


def ensure_migrated(project_dir: Path) -> None:
    """Lazily migrate a project if it still uses the legacy sampler-track shape."""
    try:
        if needs_migration(project_dir):
            migrate_project(project_dir)
    except Exception:
        # Migration is best-effort; never block a read/render on it.
        pass
