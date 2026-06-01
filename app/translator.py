"""Core translation engine wrapping PDFMathTranslate (pdf2zh).

Responsibilities
----------------
1. Lazily load and share the ONNX document-layout model across threads.
2. Split a (possibly very long) PDF into page-aligned chunks so that no single
   request is unbounded in size.
3. Translate every chunk *concurrently* via DeepSeek, while pdf2zh additionally
   parallelises paragraph-level requests inside each chunk (``thread``).
4. Re-assemble the translated chunks back together in the original page order.

Why splitting on page boundaries keeps the layout intact
--------------------------------------------------------
pdf2zh reconstructs translated text in-place, page by page, reusing the original
layout boxes and font metrics (so headings stay large and body text stays small).
By only ever cutting *between* pages we never split a page's layout, therefore
the formatting of every page is preserved exactly; merging is a pure
concatenation that restores the original order.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import fitz  # PyMuPDF

from pdf2zh.doclayout import OnnxModel
from pdf2zh.high_level import translate_stream

# --------------------------------------------------------------------------- #
# Shared layout model (downloaded once on first use, then cached on disk).
# --------------------------------------------------------------------------- #
_model = None
_model_lock = threading.Lock()


def get_model() -> OnnxModel:
    """Return a process-wide singleton of the ONNX layout model.

    onnxruntime's ``InferenceSession.run`` is thread-safe, so the single
    instance can be shared by all concurrent chunk workers.
    """
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                _model = OnnxModel.load_available()
    return _model


def model_ready() -> bool:
    """Best-effort check used by the /api/health endpoint."""
    try:
        get_model()
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# PDF splitting / merging (offline, no model or network required).
# --------------------------------------------------------------------------- #
@dataclass
class Chunk:
    index: int
    start: int  # inclusive, 0-based
    end: int    # exclusive
    data: bytes

    @property
    def pages(self) -> int:
        return self.end - self.start


def page_count(data: bytes) -> int:
    with fitz.open(stream=data, filetype="pdf") as doc:
        return doc.page_count


def plan_chunks(total_pages: int, chunk_size: int, max_chunks: int) -> List[Tuple[int, int]]:
    """Decide page ranges so that we get fast parallelism without tiny chunks.

    We never exceed ``max_chunks`` chunks (to bound concurrency), and we never
    make a chunk smaller than necessary. Returns a list of ``(start, end)``.
    """
    if total_pages <= 0:
        return []
    chunk_size = max(1, chunk_size)
    # If using the requested chunk_size would create more chunks than we are
    # willing to run, grow the chunk size so we land at ~max_chunks chunks.
    import math

    n_chunks = math.ceil(total_pages / chunk_size)
    if max_chunks and n_chunks > max_chunks:
        chunk_size = math.ceil(total_pages / max_chunks)
    ranges = []
    for start in range(0, total_pages, chunk_size):
        ranges.append((start, min(start + chunk_size, total_pages)))
    return ranges


def split_pdf(data: bytes, chunk_size: int, max_chunks: int) -> List[Chunk]:
    chunks: List[Chunk] = []
    with fitz.open(stream=data, filetype="pdf") as doc:
        total = doc.page_count
        for i, (start, end) in enumerate(plan_chunks(total, chunk_size, max_chunks)):
            sub = fitz.open()
            sub.insert_pdf(doc, from_page=start, to_page=end - 1)
            chunk_bytes = sub.tobytes(deflate=True, garbage=3)
            sub.close()
            chunks.append(Chunk(index=i, start=start, end=end, data=chunk_bytes))
    return chunks


def merge_pdfs(parts: List[bytes]) -> bytes:
    """Concatenate PDF byte blobs in the given order into one PDF."""
    out = fitz.open()
    try:
        for part in parts:
            with fitz.open(stream=part, filetype="pdf") as d:
                out.insert_pdf(d)
        return out.tobytes(deflate=True, garbage=3)
    finally:
        out.close()


# --------------------------------------------------------------------------- #
# Orchestration: concurrent translation of all chunks, ordered re-assembly.
# --------------------------------------------------------------------------- #
ProgressCb = Callable[[int, int], None]  # (pages_done, pages_total)


def translate_pdf(
    data: bytes,
    *,
    api_key: str,
    model_name: str = "deepseek-chat",
    lang_in: str = "en",
    lang_out: str = "zh",
    chunk_size: int = 8,
    concurrency: int = 6,
    thread: int = 4,
    progress_cb: Optional[ProgressCb] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Tuple[bytes, bytes]:
    """Translate a PDF and return ``(mono_pdf, dual_pdf)`` byte blobs.

    ``mono_pdf``  -> translated-only document (same page count as input).
    ``dual_pdf``  -> bilingual document (original + translation interleaved).
    """
    if not api_key:
        raise ValueError("DeepSeek API key is required")

    try:
        model = get_model()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "Failed to load the document-layout model. The first run needs "
            "internet access to download it (see scripts/prefetch.py). "
            f"Underlying error: {exc}"
        ) from exc
    envs = {"DEEPSEEK_API_KEY": api_key, "DEEPSEEK_MODEL": model_name}

    chunks = split_pdf(data, chunk_size=chunk_size, max_chunks=concurrency * 4 or 1)
    if not chunks:
        raise ValueError("The uploaded PDF has no pages")

    total_pages = sum(c.pages for c in chunks)
    results: List[Optional[Tuple[bytes, bytes]]] = [None] * len(chunks)

    state_lock = threading.Lock()
    pages_done = {"n": 0}

    def report():
        if progress_cb:
            progress_cb(pages_done["n"], total_pages)

    def run_chunk(chunk: Chunk) -> Tuple[bytes, bytes]:
        # Translate one chunk. pdf2zh reports progress through a tqdm-like
        # object whose ``.n`` is the number of pages completed *within* this
        # chunk; we translate that into a global page counter.
        seen = {"n": 0}

        def cb(t):
            if cancel_event and cancel_event.is_set():
                return
            try:
                current = int(t.n)
            except Exception:
                return
            with state_lock:
                delta = current - seen["n"]
                if delta > 0:
                    seen["n"] = current
                    pages_done["n"] += delta
                    report()

        mono, dual = translate_stream(
            chunk.data,
            lang_in=lang_in,
            lang_out=lang_out,
            service="deepseek",
            thread=thread,
            model=model,
            envs=envs,
            callback=cb,
            cancellation_event=cancel_event,
        )

        # Make sure every page of the chunk is accounted for even if the final
        # callback tick was missed.
        with state_lock:
            remaining = chunk.pages - seen["n"]
            if remaining > 0:
                pages_done["n"] += remaining
                report()
        return mono, dual

    report()  # emit an initial 0/total

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {pool.submit(run_chunk, c): c.index for c in chunks}
        for fut in as_completed(futures):
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("cancelled")
            idx = futures[fut]
            results[idx] = fut.result()  # propagates any worker exception

    mono_parts = [r[0] for r in results if r is not None]
    dual_parts = [r[1] for r in results if r is not None]
    return merge_pdfs(mono_parts), merge_pdfs(dual_parts)
