# video_annote/__main__.py
from __future__ import annotations

from .app import run_app


def main() -> int:
    return run_app()


if __name__ == "__main__":
    raise SystemExit(main())