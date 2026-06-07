"""Built-in DSP effect processors — operate on (n_frames, n_channels) float
buffers using numpy + scipy.signal. Dispatched by `process()` at export time.

These are deliberately compact, "good enough for offline rendering"
implementations rather than reference-quality plugin emulations — they cover
every parameter in SPEC.md's FX table with audible, controllable behavior.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy import signal

from ..errors import EngineError


def _db_to_linear(db: float) -> float:
    return float(10.0 ** (db / 20.0))


def _ensure_2d(buf: np.ndarray) -> np.ndarray:
    return buf if buf.ndim == 2 else buf.reshape(-1, 1)


# ---------------------------------------------------------------------------
# Reverb — Schroeder topology: parallel combs -> series allpasses
# ---------------------------------------------------------------------------

_COMB_TUNINGS_MS = (29.7, 37.1, 41.1, 43.7)
_ALLPASS_TUNINGS_MS = (5.0, 1.7)


def _comb_filter(x: np.ndarray, delay: int, feedback: float, damping: float) -> np.ndarray:
    if delay <= 0:
        return x
    out = np.zeros_like(x)
    buf = np.zeros(delay, dtype=x.dtype)
    filter_state = 0.0
    for i in range(len(x)):
        delayed = buf[i % delay]
        filter_state = delayed * (1 - damping) + filter_state * damping
        out[i] = x[i] + filter_state * feedback
        buf[i % delay] = out[i]
    return out


def _allpass_filter(x: np.ndarray, delay: int, gain: float = 0.5) -> np.ndarray:
    if delay <= 0:
        return x
    out = np.zeros_like(x)
    buf = np.zeros(delay, dtype=x.dtype)
    for i in range(len(x)):
        delayed = buf[i % delay]
        out[i] = -gain * x[i] + delayed
        buf[i % delay] = x[i] + gain * out[i]
    return out


def process_reverb(buf: np.ndarray, p: dict[str, Any], *, sample_rate: int, **_) -> np.ndarray:
    room, damping, wet = p["room"], p["damping"], p["wet"]
    feedback = 0.28 + room * 0.7

    wet_signal = np.zeros_like(buf)
    for ch in range(buf.shape[1]):
        x = buf[:, ch]
        combined = np.zeros_like(x)
        for tuning in _COMB_TUNINGS_MS:
            delay = max(int(tuning * (1 + room) / 1000.0 * sample_rate), 1)
            combined = combined + _comb_filter(x, delay, feedback, damping)
        combined /= len(_COMB_TUNINGS_MS)
        for tuning in _ALLPASS_TUNINGS_MS:
            delay = max(int(tuning / 1000.0 * sample_rate), 1)
            combined = _allpass_filter(combined, delay)
        wet_signal[:, ch] = combined

    return buf * (1 - wet) + wet_signal * wet


# ---------------------------------------------------------------------------
# Delay — BPM-synced feedback delay line
# ---------------------------------------------------------------------------

def process_delay(buf: np.ndarray, p: dict[str, Any], *, sample_rate: int, bpm: float, **_) -> np.ndarray:
    time_beats, feedback, wet = p["time"], p["feedback"], p["wet"]
    delay_samples = max(int(time_beats * (60.0 / bpm) * sample_rate), 1)

    out = buf.copy()
    n = len(buf)
    tap = np.zeros_like(buf)
    tap[delay_samples:] = buf[:n - delay_samples]

    wet_signal = np.zeros_like(buf)
    feedback_buf = tap.copy()
    gain = 1.0
    for _tap_index in range(8):
        wet_signal += feedback_buf * gain
        gain *= feedback
        if gain < 1e-3:
            break
        shifted = np.zeros_like(feedback_buf)
        shifted[delay_samples:] = feedback_buf[:n - delay_samples]
        feedback_buf = shifted

    return out * (1 - wet) + wet_signal * wet


# ---------------------------------------------------------------------------
# Compressor — feedforward, dB-domain envelope follower
# ---------------------------------------------------------------------------

def process_compressor(buf: np.ndarray, p: dict[str, Any], *, sample_rate: int, **_) -> np.ndarray:
    threshold, ratio, attack_ms, release_ms = p["threshold"], p["ratio"], p["attack"], p["release"]
    attack_coeff = np.exp(-1.0 / (max(attack_ms, 0.1) / 1000.0 * sample_rate))
    release_coeff = np.exp(-1.0 / (max(release_ms, 0.1) / 1000.0 * sample_rate))

    rectified = np.max(np.abs(buf), axis=1)
    envelope = np.zeros_like(rectified)
    level = 0.0
    for i, sample in enumerate(rectified):
        coeff = attack_coeff if sample > level else release_coeff
        level = coeff * level + (1 - coeff) * sample
        envelope[i] = level

    with np.errstate(divide="ignore"):
        env_db = 20.0 * np.log10(np.maximum(envelope, 1e-9))
    over = np.maximum(env_db - threshold, 0.0)
    gain_db = -over * (1.0 - 1.0 / ratio)
    gain_lin = 10.0 ** (gain_db / 20.0)

    return buf * gain_lin[:, None]


# ---------------------------------------------------------------------------
# EQ — low shelf + mid peak + high shelf (RBJ Audio EQ Cookbook biquads)
# ---------------------------------------------------------------------------

def _shelf_coeffs(kind: str, freq: float, gain_db: float, sample_rate: int, slope: float = 1.0):
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * np.pi * freq / sample_rate
    alpha = np.sin(w0) / 2 * np.sqrt((A + 1 / A) * (1 / slope - 1) + 2)
    cos_w0 = np.cos(w0)
    sqrt_a = np.sqrt(A)

    if kind == "low":
        b0 = A * ((A + 1) - (A - 1) * cos_w0 + 2 * sqrt_a * alpha)
        b1 = 2 * A * ((A - 1) - (A + 1) * cos_w0)
        b2 = A * ((A + 1) - (A - 1) * cos_w0 - 2 * sqrt_a * alpha)
        a0 = (A + 1) + (A - 1) * cos_w0 + 2 * sqrt_a * alpha
        a1 = -2 * ((A - 1) + (A + 1) * cos_w0)
        a2 = (A + 1) + (A - 1) * cos_w0 - 2 * sqrt_a * alpha
    else:
        b0 = A * ((A + 1) + (A - 1) * cos_w0 + 2 * sqrt_a * alpha)
        b1 = -2 * A * ((A - 1) + (A + 1) * cos_w0)
        b2 = A * ((A + 1) + (A - 1) * cos_w0 - 2 * sqrt_a * alpha)
        a0 = (A + 1) - (A - 1) * cos_w0 + 2 * sqrt_a * alpha
        a1 = 2 * ((A - 1) - (A + 1) * cos_w0)
        a2 = (A + 1) - (A - 1) * cos_w0 - 2 * sqrt_a * alpha

    return np.array([b0, b1, b2]) / a0, np.array([a0, a1, a2]) / a0


def _peak_coeffs(freq: float, gain_db: float, sample_rate: int, q: float = 1.0):
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * np.pi * freq / sample_rate
    alpha = np.sin(w0) / (2 * q)
    cos_w0 = np.cos(w0)

    b0 = 1 + alpha * A
    b1 = -2 * cos_w0
    b2 = 1 - alpha * A
    a0 = 1 + alpha / A
    a1 = -2 * cos_w0
    a2 = 1 - alpha / A
    return np.array([b0, b1, b2]) / a0, np.array([a0, a1, a2]) / a0


def process_eq(buf: np.ndarray, p: dict[str, Any], *, sample_rate: int, **_) -> np.ndarray:
    out = buf.copy()
    nyquist = sample_rate / 2.0

    if p["low_shelf"] != 0:
        b, a = _shelf_coeffs("low", min(200.0, nyquist * 0.4), p["low_shelf"], sample_rate)
        out = signal.lfilter(b, a, out, axis=0)
    if p["mid_gain"] != 0:
        freq = min(max(p["mid_freq"], 20.0), nyquist * 0.95)
        b, a = _peak_coeffs(freq, p["mid_gain"], sample_rate)
        out = signal.lfilter(b, a, out, axis=0)
    if p["high_shelf"] != 0:
        b, a = _shelf_coeffs("high", min(4000.0, nyquist * 0.8), p["high_shelf"], sample_rate)
        out = signal.lfilter(b, a, out, axis=0)

    return out


# ---------------------------------------------------------------------------
# Chorus — modulated delay line
# ---------------------------------------------------------------------------

def process_chorus(buf: np.ndarray, p: dict[str, Any], *, sample_rate: int, **_) -> np.ndarray:
    rate, depth, wet = p["rate"], p["depth"], p["wet"]
    n = len(buf)
    base_delay_ms = 15.0
    depth_ms = depth * 10.0

    t = np.arange(n) / sample_rate
    lfo = (np.sin(2 * np.pi * rate * t) + 1) / 2.0
    delay_samples = (base_delay_ms + depth_ms * lfo) / 1000.0 * sample_rate

    src_index = np.arange(n) - delay_samples
    src_index = np.clip(src_index, 0, n - 1)
    idx_floor = np.floor(src_index).astype(int)
    idx_ceil = np.minimum(idx_floor + 1, n - 1)
    frac = (src_index - idx_floor)[:, None]

    wet_signal = buf[idx_floor] * (1 - frac) + buf[idx_ceil] * frac
    return buf * (1 - wet) + wet_signal * wet


# ---------------------------------------------------------------------------
# Bitcrusher — amplitude quantization + sample-and-hold rate reduction
# ---------------------------------------------------------------------------

def process_bitcrusher(buf: np.ndarray, p: dict[str, Any], **_) -> np.ndarray:
    bits, rate_reduction = int(p["bits"]), int(p["rate_reduction"])

    levels = 2 ** bits
    quantized = np.round(buf * (levels / 2)) / (levels / 2)
    quantized = np.clip(quantized, -1.0, 1.0)

    if rate_reduction > 1:
        n = len(quantized)
        held = quantized.copy()
        for start in range(0, n, rate_reduction):
            end = min(start + rate_reduction, n)
            held[start:end] = quantized[start]
        quantized = held

    return quantized


# ---------------------------------------------------------------------------
# Filter — lp/hp/bp via Butterworth biquad, resonance maps to Q
# ---------------------------------------------------------------------------

_FILTER_BTYPE = {"lp": "low", "hp": "high", "bp": "band"}


def process_filter(buf: np.ndarray, p: dict[str, Any], *, sample_rate: int, **_) -> np.ndarray:
    filter_type, cutoff, resonance = p["type"], p["cutoff"], p["resonance"]
    nyquist = sample_rate / 2.0
    cutoff = min(max(cutoff, 1.0), nyquist * 0.999)
    q = 0.5 + resonance * 9.5

    w0 = 2 * np.pi * cutoff / sample_rate
    alpha = np.sin(w0) / (2 * q)
    cos_w0 = np.cos(w0)

    if filter_type == "lp":
        b0 = (1 - cos_w0) / 2
        b1 = 1 - cos_w0
        b2 = (1 - cos_w0) / 2
    elif filter_type == "hp":
        b0 = (1 + cos_w0) / 2
        b1 = -(1 + cos_w0)
        b2 = (1 + cos_w0) / 2
    else:  # bp (constant skirt gain)
        b0 = q * alpha
        b1 = 0.0
        b2 = -q * alpha

    a0 = 1 + alpha
    a1 = -2 * cos_w0
    a2 = 1 - alpha

    b = np.array([b0, b1, b2]) / a0
    a = np.array([1.0, a1 / a0, a2 / a0])
    return signal.lfilter(b, a, buf, axis=0)


# ---------------------------------------------------------------------------
# Limiter — lookahead peak limiter with smoothed gain reduction
# ---------------------------------------------------------------------------

def process_limiter(buf: np.ndarray, p: dict[str, Any], *, sample_rate: int, **_) -> np.ndarray:
    ceiling_lin = _db_to_linear(p["ceiling"])
    lookahead_samples = max(int(p["lookahead"] / 1000.0 * sample_rate), 1)

    peak = np.max(np.abs(buf), axis=1)
    # Lookahead: gain at sample i depends on the peak over [i, i+lookahead).
    kernel_len = lookahead_samples
    padded = np.concatenate([peak, np.zeros(kernel_len)])
    windowed_max = np.maximum.reduce([padded[i:i + len(peak)] for i in range(kernel_len)] + [peak])

    with np.errstate(divide="ignore", invalid="ignore"):
        target_gain = np.where(windowed_max > ceiling_lin, ceiling_lin / np.maximum(windowed_max, 1e-9), 1.0)

    # Smooth the gain curve so reduction doesn't introduce clicks.
    release_coeff = np.exp(-1.0 / (0.05 * sample_rate))
    smoothed = np.empty_like(target_gain)
    level = 1.0
    for i, g in enumerate(target_gain):
        if g < level:
            level = g
        else:
            level = release_coeff * level + (1 - release_coeff) * g
        smoothed[i] = level

    return buf * smoothed[:, None]


PROCESSORS = {
    "reverb": process_reverb,
    "delay": process_delay,
    "compressor": process_compressor,
    "eq": process_eq,
    "chorus": process_chorus,
    "bitcrusher": process_bitcrusher,
    "filter": process_filter,
    "limiter": process_limiter,
}


def process(fx_type: str, buf: np.ndarray, params: dict[str, Any], *,
            sample_rate: int, bpm: float | None = None) -> np.ndarray:
    fn = PROCESSORS.get(fx_type)
    if fn is None:
        raise EngineError(f"no DSP processor registered for FX type '{fx_type}'")
    buf = _ensure_2d(np.asarray(buf, dtype=np.float64))
    return fn(buf, params, sample_rate=sample_rate, bpm=bpm)
