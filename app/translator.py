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

import asyncio
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import fitz  # PyMuPDF

from pdf2zh.doclayout import OnnxModel
from pdf2zh.high_level import translate_stream


# --------------------------------------------------------------------------- #
# numpy compatibility shim.
# --------------------------------------------------------------------------- #
# BabelDOC still calls np.fromstring(pix.samples, np.uint8) in its layout /
# table / ocr code, but numpy >= 2.0 removed the binary mode of fromstring and
# now raises ("The binary mode of fromstring is removed, use frombuffer
# instead"). Transparently route binary calls to np.frombuffer -- behaviour is
# identical, so this is a pure fix with no regression. (pdf2zh already uses
# frombuffer, so it is unaffected.)
def _install_numpy_fromstring_shim() -> None:
    import numpy as np

    if getattr(np.fromstring, "_rwc_patched", False):
        return
    _orig = np.fromstring

    def fromstring(string, dtype=float, count=-1, sep=""):
        if sep == "" and isinstance(string, (bytes, bytearray, memoryview)):
            return np.frombuffer(string, dtype=dtype, count=count)
        return _orig(string, dtype=dtype, count=count, sep=sep)

    fromstring._rwc_patched = True
    np.fromstring = fromstring


_install_numpy_fromstring_shim()

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
# "Translate text inside figures/tables" toggle (pdf2zh engine).
# --------------------------------------------------------------------------- #
# By default pdf2zh leaves regions the layout model classifies as figure / table
# / formula untranslated (it paints them "preserve"). We only ever unlock
# pictures and tables -- never formulas -- so math stays intact.
_UNLOCK_CLASSES = {"figure", "table"}
_PRESERVE_CLASSES = {"abandon", "figure", "table", "isolate_formula", "formula_caption"}


class _FigureUnlockModel:
    """Layout-model proxy that relabels figure/table regions as body text.

    pdf2zh decides what to translate from the layout model's per-region class.
    By rewriting figure/table detections to a translatable text class *before*
    pdf2zh consumes them, the selectable text inside those regions gets
    translated too. Raster photos still contain no selectable text (so they are
    unaffected), and formulas are deliberately left untouched.

    Everything except ``predict`` is delegated to the wrapped model, so the
    proxy is a drop-in replacement for ``OnnxModel``.
    """

    def __init__(self, base: OnnxModel):
        self._base = base

    def __getattr__(self, name):
        return getattr(self._base, name)

    @staticmethod
    def _text_class(names: dict):
        for want in ("plain text", "plain_text", "text", "title"):
            for idx, nm in names.items():
                if nm == want:
                    return idx
        for idx, nm in names.items():  # any non-preserved class as a fallback
            if nm not in _PRESERVE_CLASSES:
                return idx
        return None

    def predict(self, *args, **kwargs):
        results = self._base.predict(*args, **kwargs)
        for res in results:
            names = getattr(res, "names", None)
            if not names:
                continue
            text_cls = self._text_class(names)
            if text_cls is None:
                continue
            for box in getattr(res, "boxes", []):
                try:
                    name = names[int(box.cls)]
                except Exception:
                    continue
                if name in _UNLOCK_CLASSES:
                    box.cls = text_cls
        return results


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
    translate_figures: bool = False,
    progress_cb: Optional[ProgressCb] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Tuple[bytes, bytes]:
    """Translate a PDF and return ``(mono_pdf, dual_pdf)`` byte blobs.

    ``mono_pdf``  -> translated-only document (same page count as input).
    ``dual_pdf``  -> bilingual document (original + translation interleaved).

    When ``translate_figures`` is set, text inside figure/table regions is
    translated too (formulas are still preserved).
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

    if translate_figures:
        model = _FigureUnlockModel(model)
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


# --------------------------------------------------------------------------- #
# Alternative engine: BabelDOC (a.k.a. pdf2zh "next" / experimental backend).
# --------------------------------------------------------------------------- #
# BabelDOC has a more advanced layout pipeline (dedicated table model, scanned
# detection, richer formula handling) and manages its own splitting/parallelism
# internally, so we hand it the whole file and let it run -- no manual
# split/merge. It is file-based and async.
_babeldoc_inited = False
_babeldoc_lock = threading.Lock()


def _ensure_babeldoc_init() -> None:
    global _babeldoc_inited
    if not _babeldoc_inited:
        with _babeldoc_lock:
            if not _babeldoc_inited:
                _install_numpy_fromstring_shim()  # BabelDOC needs the shim
                import babeldoc.high_level as bh

                bh.init()  # one-time asset/model setup (needs network first run)
                _babeldoc_inited = True


def translate_pdf_babeldoc(
    data: bytes,
    *,
    api_key: str,
    model_name: str = "deepseek-chat",
    lang_in: str = "en",
    lang_out: str = "zh",
    concurrency: int = 4,
    progress_cb: Optional[ProgressCb] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Tuple[bytes, bytes]:
    """Translate a PDF using the BabelDOC engine. Returns ``(mono, dual)`` bytes."""
    if not api_key:
        raise ValueError("DeepSeek API key is required")

    import babeldoc.high_level as bh
    from babeldoc.translation_config import TranslationConfig
    from pdf2zh.high_level import download_remote_fonts
    from pdf2zh.translator import DeepseekTranslator

    try:
        _ensure_babeldoc_init()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "Failed to initialise the BabelDOC engine (first run downloads "
            f"models/fonts and needs internet). Underlying error: {exc}"
        ) from exc

    envs = {"DEEPSEEK_API_KEY": api_key, "DEEPSEEK_MODEL": model_name}
    translator = DeepseekTranslator(lang_in, lang_out, model_name, envs=envs)
    font_path = download_remote_fonts(lang_out.lower())

    total_pages = page_count(data)

    with tempfile.TemporaryDirectory() as tmp:
        in_path = os.path.join(tmp, "input.pdf")
        out_dir = os.path.join(tmp, "out")
        os.makedirs(out_dir, exist_ok=True)
        with open(in_path, "wb") as fh:
            fh.write(data)

        config = TranslationConfig(
            translator=translator,
            input_file=in_path,
            lang_in=lang_in,
            lang_out=lang_out,
            doc_layout_model=None,  # BabelDOC loads its own if None
            font=font_path,
            output_dir=out_dir,
            no_dual=False,
            no_mono=False,
            qps=max(1, concurrency),
        )

        result = {}

        async def _run():
            async for event in bh.async_translate(config):
                if cancel_event and cancel_event.is_set():
                    raise RuntimeError("cancelled")
                etype = event.get("type")
                if etype == "progress_update" and progress_cb:
                    pct = float(event.get("overall_progress", 0) or 0)
                    progress_cb(int(total_pages * pct / 100), total_pages)
                elif etype == "error":
                    raise RuntimeError(str(event.get("error") or "BabelDOC error"))
                elif etype == "finish":
                    result["r"] = event["translate_result"]

        asyncio.run(_run())

        r = result.get("r")
        if r is None:
            raise RuntimeError("BabelDOC finished without producing a result")

        def _read(path):
            return open(path, "rb").read() if path and os.path.exists(path) else b""

        mono = _read(getattr(r, "mono_pdf_path", None))
        dual = _read(getattr(r, "dual_pdf_path", None))
        if progress_cb:
            progress_cb(total_pages, total_pages)
        return mono, dual
