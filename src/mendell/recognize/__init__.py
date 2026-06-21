"""Pluggable content-based recognition for the sample library.

`library.guess_category()` is filename-only and cheap. This package adds an
optional *recognizer* seam: given a batch of files (already probed for
duration/kind), a backend listens to the audio and returns a coarse
``category`` plus a multi-valued ``instruments`` list. Recognizers are
batch-first because model backends (CLAP, ACE-Step) amortize load
over a folder.

Three backends, selected by name via ``--recognize <backend>`` on
``library add``/``library scan`` or the ``library.recognizer`` config default:

  * ``heuristic``  — local, zero extra deps, spectral-feature rules.
  * ``clap``       — local zero-shot audio-text embedding (opt-in extra).
  * ``ace-step``   — local ACE-Step audio embedding (opt-in extra).

See ``recognize.types`` for the ``Recognizer``/``FileProbe``/``Recognition``
shapes and ``recognize.registry`` for backend lookup.
"""

from .registry import get_recognizer, list_backends
from .types import FileProbe, Recognition, Recognizer

__all__ = ["FileProbe", "Recognition", "Recognizer", "get_recognizer", "list_backends"]
