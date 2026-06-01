"""Offline tests for the split / plan / merge logic.

These do NOT require the layout model, network, or a DeepSeek key -- they only
exercise the PDF chunking and ordered re-assembly, which is the part most likely
to silently corrupt page order.
"""

import sys
from pathlib import Path

import fitz  # PyMuPDF

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import translator  # noqa: E402


def _make_pdf(n_pages: int) -> bytes:
    """Build an n-page PDF where page i prints a unique marker 'PAGE-i'."""
    doc = fitz.open()
    for i in range(n_pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"PAGE-{i}", fontsize=24)
    data = doc.tobytes()
    doc.close()
    return data


def _markers(data: bytes):
    out = []
    with fitz.open(stream=data, filetype="pdf") as doc:
        for page in doc:
            out.append(page.get_text().strip())
    return out


def test_plan_chunks_respects_max():
    ranges = translator.plan_chunks(total_pages=100, chunk_size=8, max_chunks=6)
    assert len(ranges) <= 6
    # Covers every page exactly once, contiguously.
    assert ranges[0][0] == 0
    assert ranges[-1][1] == 100
    for (a_start, a_end), (b_start, _b_end) in zip(ranges, ranges[1:]):
        assert a_end == b_start


def test_plan_chunks_small_doc():
    ranges = translator.plan_chunks(total_pages=3, chunk_size=8, max_chunks=6)
    assert ranges == [(0, 3)]


def test_split_preserves_pages():
    data = _make_pdf(23)
    chunks = translator.split_pdf(data, chunk_size=5, max_chunks=100)
    # Every page accounted for, in order.
    assert sum(c.pages for c in chunks) == 23
    rebuilt = []
    for c in chunks:
        rebuilt.extend(_markers(c.data))
    assert rebuilt == [f"PAGE-{i}" for i in range(23)]


def test_merge_restores_order():
    data = _make_pdf(17)
    chunks = translator.split_pdf(data, chunk_size=4, max_chunks=100)
    # Merge chunk bytes back (in order) and confirm identical page sequence.
    merged = translator.merge_pdfs([c.data for c in chunks])
    assert _markers(merged) == [f"PAGE-{i}" for i in range(17)]


def test_merge_is_order_sensitive():
    """Sanity check that our marker scheme actually detects mis-ordering."""
    data = _make_pdf(6)
    chunks = translator.split_pdf(data, chunk_size=2, max_chunks=100)
    reversed_parts = [c.data for c in reversed(chunks)]
    merged = translator.merge_pdfs(reversed_parts)
    assert _markers(merged) != [f"PAGE-{i}" for i in range(6)]


class _FakeBox:
    def __init__(self, cls):
        self.cls = cls
        self.xyxy = [0, 0, 1, 1]
        self.conf = 0.9


class _FakeResult:
    def __init__(self, names, classes):
        self.names = names
        self.boxes = [_FakeBox(c) for c in classes]


class _FakeBase:
    """Stand-in layout model returning preset detections."""

    NAMES = {0: "title", 1: "plain text", 2: "abandon", 3: "figure",
             4: "figure_caption", 5: "table", 8: "isolate_formula"}

    def __init__(self, classes):
        self._classes = classes

    def predict(self, *a, **k):
        return [_FakeResult(self.NAMES, self._classes)]


def test_figure_unlock_relabels_figure_and_table_only():
    # figure(3) and table(5) -> body text(1); formula(8) and text(1) untouched.
    base = _FakeBase([3, 5, 8, 1])
    proxy = translator._FigureUnlockModel(base)
    res = proxy.predict()[0]
    got = [int(b.cls) for b in res.boxes]
    names = [res.names[c] for c in got]
    assert names[0] == "plain text"   # was figure
    assert names[1] == "plain text"   # was table
    assert names[2] == "isolate_formula"  # formula preserved
    assert names[3] == "plain text"   # already text, unchanged


def test_figure_unlock_delegates_unknown_attrs():
    base = _FakeBase([3])
    base.stride = 32  # arbitrary attribute that must pass through
    proxy = translator._FigureUnlockModel(base)
    assert proxy.stride == 32


def test_numpy_fromstring_shim_handles_binary():
    # The shim (installed on import of app.translator) must make BabelDOC's
    # np.fromstring(bytes, uint8) pattern work on numpy >= 2.0.
    import numpy as np

    arr = np.fromstring(b"\x01\x02\x03\x04", np.uint8)
    assert arr.tolist() == [1, 2, 3, 4]
    # Text mode must still behave normally.
    assert np.fromstring("1 2 3", dtype=int, sep=" ").tolist() == [1, 2, 3]


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
