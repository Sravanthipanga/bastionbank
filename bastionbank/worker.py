"""Standalone queue worker:  python -m bastionbank.worker

The FastAPI app also runs this loop in-process; this CLI mode is for the
production topology where the worker runs as its own process.
"""
from __future__ import annotations

import time

from . import queue as queue_mod
from .config import load_config
from .store import open_store


def main() -> None:
    config = load_config()
    store = open_store(config)
    print(f"bastionbank worker polling {config.db_path} every {config.worker_poll_s}s")
    while True:
        attempted = queue_mod.process_available_jobs(
            store, backoff_base_s=config.job_backoff_base_s)
        if attempted:
            print(f"processed {attempted} job(s)")
        time.sleep(config.worker_poll_s)


if __name__ == "__main__":
    main()
