"""Native-BPM and warp-mode auto-detection for imported audio clips.

Two-stage pipeline per SPEC.md: instant filename-keyword matching first, then
signal analysis (cached — runs once per import) as a fallback. Detection is
implemented with `librosa` alone; `aubio` is listed in the spec's tech-stack
table but is functionally redundant with librosa for tempo/onset estimation
and requires native build toolchains that aren't reliably available, so this
implementation standardizes on librosa for both BPM and warp detection.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import librosa
import numpy as np

WARP_MODES = ("beats", "melodic", "harmonic", "vocal", "complex")

_FILENAME_WARP_KEYWORDS: dict[str, tuple[str, ...]] = {
    "beats": ("drum", "loop", "beat", "perc", "hat", "kick", "snare"),
    "melodic": ("bass", "lead", "melody", "arp", "mono"),
    "harmonic": ("pad", "chord", "keys", "synth", "harm", "atmo"),
    "vocal": ("vox", "vocal", "voice", "acap", "sing"),
}

# e.g. "loop_135bpm.wav", "drums-128-bpm.wav", "perc 90.wav"
_BPM_FILENAME_RE = re.compile(r"(\d{2,3}(?:\.\d+)?)\s*[-_]?\s*bpm|bpm\s*[-_]?\s*(\d{2,3}(?:\.\d+)?)", re.I)
_BARE_NUMBER_RE = re.compile(r"(?<![\d.])(\d{2,3})(?![\d.])")


def detect_warp_from_filename(filename: str) -> str | None:
    stem = Path(filename).stem.lower()
    for mode, keywords in _FILENAME_WARP_KEYWORDS.items():
        if any(kw in stem for kw in keywords):
            return mode
    return None


def detect_bpm_from_filename(filename: str) -> float | None:
    stem = Path(filename).stem.lower()
    m = _BPM_FILENAME_RE.search(stem)
    if m:
        value = m.group(1) or m.group(2)
        return float(value)
    # Fall back to a bare 2-3 digit number in plausible BPM range.
    m = _BARE_NUMBER_RE.search(stem)
    if m:
        value = float(m.group(1))
        if 40.0 <= value <= 220.0:
            return value
    return None


class _AnalysisCache:
    """Loads and analyzes the audio signal once; subsequent lookups are free."""

    def __init__(self, path: str):
        self.path = path
        self._y: np.ndarray | None = None
        self._sr: int | None = None
        self._tempo: float | None = None
        self._onset_env: np.ndarray | None = None
        self._harmonic: np.ndarray | None = None
        self._percussive: np.ndarray | None = None
        self._f0: np.ndarray | None = None
        self._voiced_ratio: float | None = None

    def _load(self):
        if self._y is None:
            self._y, self._sr = librosa.load(self.path, sr=None, mono=True)

    @property
    def y(self) -> np.ndarray:
        self._load()
        return self._y

    @property
    def sr(self) -> int:
        self._load()
        return self._sr

    def tempo(self) -> float:
        if self._tempo is None:
            onset_env = self._onset_envelope()
            tempo = librosa.feature.tempo(onset_envelope=onset_env, sr=self.sr)
            self._tempo = float(np.atleast_1d(tempo)[0])
        return self._tempo

    def _onset_envelope(self) -> np.ndarray:
        if self._onset_env is None:
            self._onset_env = librosa.onset.onset_strength(y=self.y, sr=self.sr)
        return self._onset_env

    def _hpss(self) -> tuple[np.ndarray, np.ndarray]:
        if self._harmonic is None:
            self._harmonic, self._percussive = librosa.effects.hpss(self.y)
        return self._harmonic, self._percussive

    def transient_density(self) -> float:
        """Onsets per second — high for drum loops/percussion."""
        onset_env = self._onset_envelope()
        onsets = librosa.onset.onset_detect(onset_envelope=onset_env, sr=self.sr)
        duration = len(self.y) / self.sr
        return len(onsets) / duration if duration > 0 else 0.0

    def percussive_ratio(self) -> float:
        """Fraction of total energy that is percussive (vs. harmonic)."""
        harmonic, percussive = self._hpss()
        h_energy = float(np.sum(harmonic ** 2))
        p_energy = float(np.sum(percussive ** 2))
        total = h_energy + p_energy
        return p_energy / total if total > 0 else 0.0

    def _pitch_track(self):
        if self._f0 is None:
            f0, voiced_flag, _ = librosa.pyin(
                self.y,
                fmin=librosa.note_to_hz("C2"),
                fmax=librosa.note_to_hz("C7"),
            )
            self._f0 = f0
            voiced = voiced_flag.astype(bool) if voiced_flag is not None else np.zeros_like(f0, dtype=bool)
            self._voiced_ratio = float(np.mean(voiced)) if len(voiced) else 0.0
        return self._f0

    def voiced_ratio(self) -> float:
        self._pitch_track()
        return self._voiced_ratio or 0.0

    def pitch_stability(self) -> float:
        """Coefficient of variation of the voiced f0 track — low means stable
        single pitch (melodic), high means many concurrent/changing pitches."""
        f0 = self._pitch_track()
        voiced = f0[~np.isnan(f0)]
        if len(voiced) < 2:
            return 1.0
        mean = float(np.mean(voiced))
        std = float(np.std(voiced))
        return std / mean if mean > 0 else 1.0

    def formant_strength(self) -> float:
        """Crude formant-structure proxy: spectral-contrast variance in the
        speech-relevant bands. Higher implies stronger formant structure."""
        contrast = librosa.feature.spectral_contrast(y=self.y, sr=self.sr)
        return float(np.mean(np.var(contrast, axis=1)))


def detect_bpm_via_analysis(path: str, cache: _AnalysisCache | None = None) -> float:
    cache = cache or _AnalysisCache(path)
    return round(cache.tempo(), 2)


def detect_warp_via_analysis(path: str, cache: _AnalysisCache | None = None) -> str:
    cache = cache or _AnalysisCache(path)

    transient_density = cache.transient_density()
    percussive_ratio = cache.percussive_ratio()
    voiced_ratio = cache.voiced_ratio()
    pitch_stability = cache.pitch_stability()
    formant_strength = cache.formant_strength()

    # Stage-2 heuristics, applied in the order described in SPEC.md.
    if transient_density > 2.5 and percussive_ratio > 0.5:
        return "beats"
    if voiced_ratio > 0.5 and formant_strength > 0.6:
        return "vocal"
    if voiced_ratio > 0.4 and pitch_stability < 0.05:
        return "melodic"
    if voiced_ratio > 0.3 and pitch_stability >= 0.05:
        return "harmonic"
    return "complex"


def analyze_clip(path: str, filename: str) -> dict[str, Any]:
    """Run the full two-stage detection pipeline for native BPM and warp mode.

    Returns {"native_bpm", "warp", "source"} where `source` records which
    stage produced the *warp* result (bpm uses the same precedence but isn't
    separately reported, per the example in SPEC.md).
    """
    warp = detect_warp_from_filename(filename)
    bpm = detect_bpm_from_filename(filename)
    source = "filename" if warp is not None else None

    if warp is None or bpm is None:
        cache = _AnalysisCache(path)
        if warp is None:
            warp = detect_warp_via_analysis(path, cache)
            source = "tempo_analysis"
        if bpm is None:
            bpm = detect_bpm_via_analysis(path, cache)
            source = source or "tempo_analysis"

    return {"native_bpm": bpm, "warp": warp, "source": source or "filename"}
