"""Unit tests for the pluggable recognition backends (`mendell.recognize`)."""

from pathlib import Path

import pytest

from mendell.errors import BadInputError
from mendell.recognize import get_recognizer, list_backends
from mendell.recognize.heuristic import HeuristicRecognizer, _classify_loop, _classify_oneshot
from mendell.recognize.types import FileProbe, Recognition


class _FakeCache:
    """Duck-typed stand-in for `_AnalysisCache` with fixed feature values —
    deterministic, no audio I/O."""

    def __init__(self, path="fake.wav", **features):
        self.path = path
        self._features = {
            "spectral_centroid": 1000.0,
            "spectral_rolloff": 2000.0,
            "zero_crossing_rate": 0.05,
            "log_attack_time": -1.0,
            "percussive_ratio": 0.3,
            "voiced_ratio": 0.0,
            "pitch_stability": 1.0,
            "formant_strength": 0.0,
        }
        self._features.update(features)

    def spectral_centroid(self):
        return self._features["spectral_centroid"]

    def spectral_rolloff(self):
        return self._features["spectral_rolloff"]

    def zero_crossing_rate(self):
        return self._features["zero_crossing_rate"]

    def log_attack_time(self):
        return self._features["log_attack_time"]

    def percussive_ratio(self):
        return self._features["percussive_ratio"]

    def voiced_ratio(self):
        return self._features["voiced_ratio"]

    def pitch_stability(self):
        return self._features["pitch_stability"]

    def formant_strength(self):
        return self._features["formant_strength"]


# --- one-shot classification -------------------------------------------------

def test_classify_oneshot_kick():
    cache = _FakeCache(spectral_centroid=150.0, zero_crossing_rate=0.02, log_attack_time=-2.0)
    assert _classify_oneshot(cache) == "kick"


def test_classify_oneshot_808_when_pitched():
    cache = _FakeCache(
        spectral_centroid=150.0, zero_crossing_rate=0.02, log_attack_time=-2.0,
        voiced_ratio=0.6, pitch_stability=0.02,
    )
    assert _classify_oneshot(cache) == "808"


def test_classify_oneshot_hat():
    cache = _FakeCache(spectral_centroid=6000.0, spectral_rolloff=9000.0, zero_crossing_rate=0.3, log_attack_time=-2.0)
    assert _classify_oneshot(cache) == "hat"


def test_classify_oneshot_snare():
    cache = _FakeCache(
        spectral_centroid=1500.0, zero_crossing_rate=0.3, log_attack_time=-2.0,
        percussive_ratio=0.8,
    )
    assert _classify_oneshot(cache) == "snare"


def test_classify_oneshot_clap():
    cache = _FakeCache(
        spectral_centroid=2200.0, zero_crossing_rate=0.3, log_attack_time=-2.0,
        percussive_ratio=0.8,
    )
    assert _classify_oneshot(cache) == "clap"


def test_classify_oneshot_vocal():
    cache = _FakeCache(voiced_ratio=0.7, pitch_stability=1.0, formant_strength=0.8)
    assert _classify_oneshot(cache) == "vocal"


def test_classify_oneshot_melody_stab():
    cache = _FakeCache(voiced_ratio=0.6, pitch_stability=0.01, formant_strength=0.1, log_attack_time=-2.0)
    assert _classify_oneshot(cache) == "stab"


def test_classify_oneshot_melody_sustained():
    cache = _FakeCache(voiced_ratio=0.6, pitch_stability=0.01, formant_strength=0.1, log_attack_time=0.0)
    assert _classify_oneshot(cache) == "melody"


def test_classify_oneshot_bass():
    cache = _FakeCache(spectral_centroid=200.0, zero_crossing_rate=0.05, log_attack_time=0.0)
    assert _classify_oneshot(cache) == "bass"


def test_classify_oneshot_perc():
    cache = _FakeCache(spectral_centroid=1500.0, zero_crossing_rate=0.05, percussive_ratio=0.7, log_attack_time=0.0)
    assert _classify_oneshot(cache) == "perc"


def test_classify_oneshot_fx_fallback():
    cache = _FakeCache(spectral_centroid=1500.0, zero_crossing_rate=0.05, percussive_ratio=0.1, log_attack_time=0.0)
    assert _classify_oneshot(cache) == "fx"


# --- loop classification (relabel detect_warp_via_analysis) ------------------

@pytest.mark.parametrize("warp,expected", [
    ("beats", "drum"),
    ("melodic", "melodic"),
    ("harmonic", "chord"),
    ("vocal", "vocal"),
    ("complex", "full"),
])
def test_classify_loop_relabels_warp(monkeypatch, warp, expected):
    monkeypatch.setattr("mendell.recognize.heuristic.audio_analysis.detect_warp_via_analysis", lambda path, cache=None: warp)
    cache = _FakeCache()
    assert _classify_loop(cache) == expected


# --- HeuristicRecognizer batch API --------------------------------------------

def test_heuristic_recognizer_batch(monkeypatch):
    fakes = {
        "kick.wav": _FakeCache("kick.wav", spectral_centroid=150.0, zero_crossing_rate=0.02, log_attack_time=-2.0),
        "hat.wav": _FakeCache("hat.wav", spectral_centroid=6000.0, spectral_rolloff=9000.0, zero_crossing_rate=0.3, log_attack_time=-2.0),
    }
    recognizer = HeuristicRecognizer(cache_provider=lambda p: fakes[p])
    items = [
        FileProbe(path=Path("kick.wav"), filename="kick.wav", duration=0.2, kind="one-shot"),
        FileProbe(path=Path("hat.wav"), filename="hat.wav", duration=0.1, kind="one-shot"),
    ]
    results = recognizer.recognize(items)
    assert results[0] == Recognition(category="kick", instruments=[], source="heuristic", confidence=0.55)
    assert results[1] == Recognition(category="hat", instruments=[], source="heuristic", confidence=0.55)


def test_heuristic_recognizer_defers_on_unreadable_file():
    recognizer = HeuristicRecognizer(cache_provider=lambda p: (_ for _ in ()).throw(OSError("bad file")))
    items = [FileProbe(path=Path("broken.wav"), filename="broken.wav", duration=None, kind="one-shot")]
    assert recognizer.recognize(items) == [None]


# --- registry -------------------------------------------------------------

def test_list_backends():
    assert list_backends() == ["clap", "gemini-embedding", "gemini-generative", "heuristic"]


def test_get_recognizer_unknown_backend_raises():
    with pytest.raises(BadInputError):
        get_recognizer("not-a-backend")


def test_get_recognizer_heuristic():
    assert isinstance(get_recognizer("heuristic"), HeuristicRecognizer)


def test_clap_backend_missing_dependency_raises_actionable_error():
    with pytest.raises(BadInputError, match=r"pip install 'mendell\[clap\]'"):
        get_recognizer("clap")


def test_gemini_embedding_missing_dependency_raises_actionable_error(monkeypatch):
    # Even with a key set, the missing SDK should be reported first.
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    with pytest.raises(BadInputError, match=r"pip install 'mendell\[gemini\]'"):
        get_recognizer("gemini-embedding")


def test_gemini_generative_missing_dependency_raises_actionable_error():
    with pytest.raises(BadInputError, match=r"pip install 'mendell\[gemini\]'"):
        get_recognizer("gemini-generative")
