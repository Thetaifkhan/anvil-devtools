"""Standalone queue worker entrypoint.

Use this in a Render Background Worker or Render Cron Job when you
don't want the worker thread running inside the web service.

Usage:
    python -m delivery.worker_cli                    # run forever
    python -m delivery.worker_cli --once             # process one batch and exit
    python -m delivery.worker_cli --once --batch 50  # larger batch

This is the recommended topology for production. The web service runs
with SUPABASE_WORKER_ENABLED=0; this CLI runs as a separate Background
Worker process so the worker is single-instance and the SQLite outbox
is shared only between the worker and the web service's enqueue side.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

# Ensure the project root is on sys.path when invoked as a module.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Anvil DevTools queue worker")
    parser.add_argument("--once", action="store_true",
                        help="Process one batch and exit (useful for cron).")
    parser.add_argument("--batch", type=int, default=10,
                        help="Batch size (1-100). Default 10.")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="Poll interval in seconds when running forever.")
    args = parser.parse_args()

    _setup_logging()
    if not (1 <= args.batch <= 100):
        print("--batch must be between 1 and 100", file=sys.stderr)
        return 2

    # Force the worker module to treat itself as enabled — we're explicitly
    # being invoked by the operator. The Background Worker never serves
    # HTTP, so the per-request path that starts the worker thread is
    # irrelevant here.
    os.environ.setdefault("SUPABASE_WORKER_ENABLED", "1")

    from delivery import worker
    from delivery.outbox import configure as configure_outbox
    configure_outbox()

    if args.once:
        processed = worker.run_once(batch_size=args.batch)
        print(f"Processed {processed} item(s).")
        return 0

    # Run forever. We don't use the daemon thread here — the CLI itself
    # is the worker process. Sleeping between batches keeps the process
    # alive (Background Workers exit when their main() returns).
    log = logging.getLogger("anvil.delivery.worker_cli")
    log.info("Worker CLI started (batch=%d, interval=%.1fs).",
             args.batch, args.interval)
    try:
        while True:
            try:
                worker.run_once(batch_size=args.batch)
            except Exception as exc:  # pragma: no cover — defensive
                log.exception("iteration failed: %s", exc)
            time.sleep(max(1.0, args.interval))
    except KeyboardInterrupt:
        log.info("stopping (interrupted).")
        return 0


if __name__ == "__main__":
    sys.exit(main())
