"""Small shared audio-buffer utilities used by the render pipeline."""

from __future__ import annotations

import numpy as np


def db_to_linear(db: float) -> float:
    return float(10.0 ** (db / 20.0))


def pan_gains(pan: int) -> tuple[float, float]:
    """Equal-power pan law: pan in [-100, 100], 0 = center -> (left, right) gains."""
    p = (pan + 100) / 200.0
    angle = p * (np.pi / 2)
    return float(np.cos(angle)), float(np.sin(angle))


def to_stereo(mono: np.ndarray) -> np.ndarray:
    if mono.ndim == 2 and mono.shape[1] == 2:
        return mono
    if mono.ndim == 2:
        mono = mono.mean(axis=1)
    return np.column_stack([mono, mono])


def resample_by_ratio(y: np.ndarray, ratio: float) -> np.ndarray:
    """Pitch-shift-by-resampling: speeds through `y` `ratio`-times faster, so
    played back at the original rate it sounds `ratio`-times higher in pitch
    (and is correspondingly shorter — the way a sampler pitches samples)."""
    if ratio == 1.0:
        return y
    n = len(y)
    new_n = max(int(round(n / ratio)), 1)
    src_idx = np.clip(np.arange(new_n) * ratio, 0, n - 1)
    idx_floor = np.floor(src_idx).astype(int)
    idx_ceil = np.minimum(idx_floor + 1, n - 1)
    frac = (src_idx - idx_floor)
    if y.ndim == 2:
        frac = frac[:, None]
    return y[idx_floor] * (1 - frac) + y[idx_ceil] * frac


def mix_at(dest: np.ndarray, src: np.ndarray, frame: int) -> None:
    """Add `src` into `dest` starting at sample `frame`, clipping to bounds."""
    if frame >= len(dest):
        return
    start = max(frame, 0)
    src_offset = start - frame
    n = min(len(src) - src_offset, len(dest) - start)
    if n <= 0:
        return
    dest[start:start + n] += src[src_offset:src_offset + n]


def adsr_envelope(
    total_frames: int, note_off_frame: int, *,
    attack_ms: float, decay_ms: float, sustain_pct: float, release_ms: float, sample_rate: int,
) -> np.ndarray:
    """Build an ADSR amplitude envelope. `note_off_frame` is where the
    sustain phase ends and release begins (i.e. the note's held duration)."""
    attack_n = int(attack_ms / 1000.0 * sample_rate)
    decay_n = int(decay_ms / 1000.0 * sample_rate)
    release_n = int(release_ms / 1000.0 * sample_rate)
    sustain_level = sustain_pct / 100.0

    env = np.zeros(total_frames)

    a_end = min(attack_n, total_frames)
    if a_end > 0:
        env[:a_end] = np.linspace(0.0, 1.0, a_end, endpoint=False)

    d_start = a_end
    d_end = min(d_start + decay_n, total_frames)
    if d_end > d_start:
        env[d_start:d_end] = np.linspace(1.0, sustain_level, d_end - d_start, endpoint=False)

    s_start = d_end
    s_end = max(min(note_off_frame, total_frames), s_start)
    if s_end > s_start:
        env[s_start:s_end] = sustain_level

    r_start = s_end
    r_end = min(r_start + release_n, total_frames)
    if r_end > r_start:
        start_level = env[r_start - 1] if r_start > 0 else sustain_level
        env[r_start:r_end] = np.linspace(start_level, 0.0, r_end - r_start, endpoint=False)

    return env
