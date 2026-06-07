"""Sampler management — sample maps, ADSR envelopes, and per-slot params.

A sampler TOML lives at samplers/<track>.toml; the hosting track must be of
type `sampler`. Slots are addressed by their mapped note (the low end of a
range counts as its identity for `set`/`remove`).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import soundfile as sf

from .. import paths
from .. import tracks as tracks_mod
from ..durations import format_duration_ms, parse_duration_ms
from ..errors import BadInputError, NotFoundError
from ..notes import midi_to_note_name, note_name_to_midi, parse_note_range
from ..toml_io import read_toml, write_toml

LOOP_MODES = ("off", "forward", "pingpong")
AUDIO_EXTS = {".wav", ".aiff", ".aif", ".flac", ".mp3", ".ogg"}

SAMPLER_DEFAULTS = {"polyphony": 8, "tune": 0}

SLOT_DEFAULTS = {
    "vol": 100,
    "pan": 0,
    "tune": 0,
    "pitch_follow": True,
    "loop": "off",
    "loop_start": 0.0,
    "attack_ms": 1.0,
    "decay_ms": 10.0,
    "sustain": 100,
    "release_ms": 50.0,
}


# ---------------------------------------------------------------------------
# load / save
# ---------------------------------------------------------------------------

def exists(project_dir: Path, track_name: str) -> bool:
    return paths.sampler_toml(project_dir, track_name).is_file()


def load(project_dir: Path, track_name: str) -> dict[str, Any]:
    toml_path = paths.sampler_toml(project_dir, track_name)
    if not toml_path.is_file():
        raise NotFoundError(f"sampler '{track_name}' not found (run `mendell sampler create` first)")
    return read_toml(toml_path)


def save(project_dir: Path, track_name: str, data: dict[str, Any]) -> None:
    write_toml(paths.sampler_toml(project_dir, track_name), data)


def _require_sampler_track(project_dir: Path, track_name: str) -> dict[str, Any]:
    track_data = tracks_mod.load(project_dir, track_name)
    if track_data.get("track", {}).get("type") != "sampler":
        raise BadInputError(f"track '{track_name}' is not a sampler track")
    return track_data


def create(project_dir: Path, track_name: str) -> dict[str, Any]:
    """Idempotent create-or-update: re-creating an existing sampler is a no-op."""
    _require_sampler_track(project_dir, track_name)
    if not exists(project_dir, track_name):
        save(project_dir, track_name, {"sampler": dict(SAMPLER_DEFAULTS), "slots": []})
    return summary(project_dir, track_name)


def summary(project_dir: Path, track_name: str) -> dict[str, Any]:
    data = load(project_dir, track_name)
    sampler = {**SAMPLER_DEFAULTS, **data.get("sampler", {})}
    return {"track": track_name, **sampler, "slots": len(data.get("slots", []))}


def show(project_dir: Path, track_name: str) -> dict[str, Any]:
    data = load(project_dir, track_name)
    sampler = {**SAMPLER_DEFAULTS, **data.get("sampler", {})}
    return {
        "track": track_name,
        "sampler": sampler,
        "slots": [_slot_to_json(s) for s in data.get("slots", [])],
    }


def set_params(project_dir: Path, track_name: str, *, polyphony: int | None = None,
               tune: int | None = None) -> dict[str, Any]:
    data = load(project_dir, track_name)
    sampler = data.setdefault("sampler", dict(SAMPLER_DEFAULTS))

    changed: dict[str, Any] = {}
    if polyphony is not None:
        polyphony = int(polyphony)
        if polyphony < 1:
            raise BadInputError(f"polyphony must be >= 1, got {polyphony}")
        sampler["polyphony"] = polyphony
        changed["polyphony"] = polyphony
    if tune is not None:
        tune = int(tune)
        if not (-100 <= tune <= 100):
            raise BadInputError(f"tune must be -100..100, got {tune}")
        sampler["tune"] = tune
        changed["tune"] = tune

    save(project_dir, track_name, data)
    return {"changed": changed, **summary(project_dir, track_name)}


# ---------------------------------------------------------------------------
# sample file handling
# ---------------------------------------------------------------------------

def _store_sample(project_dir: Path, src_path: str, *, link: bool) -> tuple[Path, float]:
    src = Path(src_path)
    if not src.is_file():
        raise BadInputError(f"sample file not found: {src_path}")
    if src.suffix.lower() not in AUDIO_EXTS:
        raise BadInputError(f"unsupported audio file type '{src.suffix}'")

    if link:
        stored = src.resolve()
    else:
        dest_dir = paths.samples_dir(project_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        if dest.resolve() != src.resolve():
            shutil.copy2(src, dest)
        stored = dest.resolve()

    info = sf.info(str(stored))
    return stored, info.frames / info.samplerate


# ---------------------------------------------------------------------------
# slot helpers
# ---------------------------------------------------------------------------

def _find_slot(slots: list[dict], note_low: int) -> dict | None:
    for slot in slots:
        if slot["note_low"] == note_low:
            return slot
    return None


def _slot_to_json(slot: dict[str, Any]) -> dict[str, Any]:
    note_low, note_high = slot["note_low"], slot["note_high"]
    out = {
        "note": midi_to_note_name(note_low),
        "range": (
            midi_to_note_name(note_low) if note_low == note_high
            else f"{midi_to_note_name(note_low)}-{midi_to_note_name(note_high)}"
        ),
        "root": midi_to_note_name(slot["root"]),
        "sample": slot["sample"],
        "linked": slot.get("linked", False),
        "vol": slot.get("vol", SLOT_DEFAULTS["vol"]),
        "pan": slot.get("pan", SLOT_DEFAULTS["pan"]),
        "tune": slot.get("tune", SLOT_DEFAULTS["tune"]),
        "pitch_follow": slot.get("pitch_follow", SLOT_DEFAULTS["pitch_follow"]),
        "loop": slot.get("loop", SLOT_DEFAULTS["loop"]),
        "loop_start": slot.get("loop_start", SLOT_DEFAULTS["loop_start"]),
        "loop_end": slot.get("loop_end"),
        "attack": format_duration_ms(slot.get("attack_ms", SLOT_DEFAULTS["attack_ms"])),
        "decay": format_duration_ms(slot.get("decay_ms", SLOT_DEFAULTS["decay_ms"])),
        "sustain": slot.get("sustain", SLOT_DEFAULTS["sustain"]),
        "release": format_duration_ms(slot.get("release_ms", SLOT_DEFAULTS["release_ms"])),
    }
    return out


def _new_slot(*, note_low, note_high, root, sample, linked, length_seconds, loop) -> dict[str, Any]:
    slot = {
        "note_low": note_low,
        "note_high": note_high,
        "root": root,
        "sample": sample,
        "linked": linked,
        "loop_end": round(length_seconds, 6),
    }
    slot.update(SLOT_DEFAULTS)
    slot["loop_end"] = round(length_seconds, 6)
    if loop:
        slot["loop"] = "forward"
    return slot


# ---------------------------------------------------------------------------
# map add / bulk import / set / list / remove
# ---------------------------------------------------------------------------

def map_add(
    project_dir: Path,
    track_name: str,
    *,
    note: str | None = None,
    note_range: str | None = None,
    sample: str,
    root: str | None = None,
    link: bool = False,
    loop: bool = False,
) -> dict[str, Any]:
    if (note is None) == (note_range is None):
        raise BadInputError("provide exactly one of --note or --range")

    data = load(project_dir, track_name)
    slots = data.setdefault("slots", [])

    if note is not None:
        note_low = note_high = note_name_to_midi(note)
    else:
        note_low, note_high = parse_note_range(note_range)

    root_midi = note_name_to_midi(root) if root is not None else note_low

    stored_path, length_seconds = _store_sample(project_dir, sample, link=link)

    existing = _find_slot(slots, note_low)
    slot = _new_slot(
        note_low=note_low, note_high=note_high, root=root_midi,
        sample=str(stored_path), linked=link, length_seconds=length_seconds, loop=loop,
    )
    if existing is not None:
        slots[slots.index(existing)] = slot
    else:
        slots.append(slot)
        slots.sort(key=lambda s: s["note_low"])

    save(project_dir, track_name, data)
    return _slot_to_json(slot)


def bulk_import(
    project_dir: Path, track_name: str, folder: str, *, start_note: str
) -> dict[str, Any]:
    folder_path = Path(folder)
    if not folder_path.is_dir():
        raise BadInputError(f"folder not found: {folder}")

    files = sorted(p for p in folder_path.iterdir() if p.suffix.lower() in AUDIO_EXTS)
    if not files:
        raise BadInputError(f"no audio files found in {folder}")

    start_midi = note_name_to_midi(start_note)
    data = load(project_dir, track_name)
    slots = data.setdefault("slots", [])

    mapped = []
    for offset, file_path in enumerate(files):
        note_midi = start_midi + offset
        if note_midi > 127:
            raise BadInputError(
                f"folder has {len(files)} files; mapping from {start_note} exceeds MIDI note 127"
            )
        stored_path, length_seconds = _store_sample(project_dir, str(file_path), link=False)
        slot = _new_slot(
            note_low=note_midi, note_high=note_midi, root=note_midi,
            sample=str(stored_path), linked=False, length_seconds=length_seconds, loop=False,
        )
        existing = _find_slot(slots, note_midi)
        if existing is not None:
            slots[slots.index(existing)] = slot
        else:
            slots.append(slot)
        mapped.append({"note": midi_to_note_name(note_midi), "sample": file_path.name})

    slots.sort(key=lambda s: s["note_low"])
    save(project_dir, track_name, data)
    return {"track": track_name, "mapped": mapped, "count": len(mapped)}


SLOT_SETTABLE = {
    "root", "vol", "pan", "tune", "pitch_follow", "loop", "loop_start",
    "loop_end", "attack", "decay", "sustain", "release",
}


def map_set(project_dir: Path, track_name: str, note: str, **kwargs: Any) -> dict[str, Any]:
    data = load(project_dir, track_name)
    slots = data.get("slots", [])
    note_low = note_name_to_midi(note)
    slot = _find_slot(slots, note_low)
    if slot is None:
        raise NotFoundError(f"no sample mapped at note '{note}'")

    changed: dict[str, Any] = {}
    for name, value in kwargs.items():
        if value is None:
            continue
        if name not in SLOT_SETTABLE:
            raise BadInputError(f"'{name}' is not a settable sampler-slot parameter")

        if name == "root":
            slot["root"] = note_name_to_midi(value)
            changed[name] = value
        elif name == "vol":
            value = int(value)
            if not (0 <= value <= 100):
                raise BadInputError(f"vol must be 0-100, got {value}")
            slot["vol"] = value
            changed[name] = value
        elif name == "pan":
            value = int(value)
            if not (-100 <= value <= 100):
                raise BadInputError(f"pan must be -100..100, got {value}")
            slot["pan"] = value
            changed[name] = value
        elif name == "tune":
            value = int(value)
            if not (-100 <= value <= 100):
                raise BadInputError(f"tune must be -100..100 cents, got {value}")
            slot["tune"] = value
            changed[name] = value
        elif name == "pitch_follow":
            slot["pitch_follow"] = bool(value)
            changed[name] = bool(value)
        elif name == "loop":
            if value not in LOOP_MODES:
                raise BadInputError(f"invalid loop mode '{value}' (expected one of {LOOP_MODES})")
            slot["loop"] = value
            changed[name] = value
        elif name in ("loop_start", "loop_end"):
            slot[name] = float(value)
            changed[name] = float(value)
        elif name in ("attack", "decay", "release"):
            ms = parse_duration_ms(value)
            slot[f"{name}_ms"] = ms
            changed[name] = format_duration_ms(ms)
        elif name == "sustain":
            value = int(value)
            if not (0 <= value <= 100):
                raise BadInputError(f"sustain must be 0-100, got {value}")
            slot["sustain"] = value
            changed[name] = value

    save(project_dir, track_name, data)
    return {"changed": changed, **_slot_to_json(slot)}


def map_list(project_dir: Path, track_name: str) -> list[dict[str, Any]]:
    data = load(project_dir, track_name)
    return [_slot_to_json(s) for s in data.get("slots", [])]


def map_remove(project_dir: Path, track_name: str, note: str) -> dict[str, Any]:
    data = load(project_dir, track_name)
    slots = data.get("slots", [])
    note_low = note_name_to_midi(note)
    slot = _find_slot(slots, note_low)
    if slot is None:
        raise NotFoundError(f"no sample mapped at note '{note}'")
    slots.remove(slot)
    save(project_dir, track_name, data)
    return {"removed": note, "track": track_name}
