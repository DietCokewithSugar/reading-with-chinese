"""Pre-download the document-layout model and fonts used by pdf2zh.

Run this once, on a machine with internet access, so the first translation does
not have to wait for the model/fonts to download:

    python3 scripts/prefetch.py
"""

import sys


def main() -> int:
    print("Downloading the ONNX document-layout model (one-time)...")
    try:
        from pdf2zh.doclayout import OnnxModel

        OnnxModel.load_available()
        print("  -> layout model ready.")
    except Exception as exc:  # noqa: BLE001
        print(f"  !! failed to fetch layout model: {exc}")
        print("     (Check network access to the model host, then retry.)")
        return 1
    print("Done. The app is ready for offline-fast first use.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
