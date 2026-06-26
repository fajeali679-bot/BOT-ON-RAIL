"""
Watchdog — auto-restarts main.py if it crashes or exits unexpectedly.
"""
import subprocess
import sys
import time
import logging
import os

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | watchdog | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

SCRIPT = os.path.join(os.path.dirname(__file__), "main.py")
RESTART_DELAY = 5
MAX_RESTARTS = 20
RESET_AFTER = 300


def run():
    restarts = 0
    last_restart_time = time.time()

    logger.info("Watchdog started — launching bots…")

    while True:
        start_time = time.time()
        proc = subprocess.run([sys.executable, SCRIPT])
        elapsed = time.time() - start_time

        if time.time() - last_restart_time > RESET_AFTER:
            restarts = 0
            last_restart_time = time.time()

        if proc.returncode == 0:
            logger.info("Bots exited cleanly (code 0). Shutting down watchdog.")
            break

        restarts += 1
        logger.warning(
            f"Bots exited with code {proc.returncode} after {elapsed:.1f}s "
            f"(restart #{restarts}/{MAX_RESTARTS})"
        )

        if restarts >= MAX_RESTARTS:
            logger.error(
                f"Reached {MAX_RESTARTS} restarts — pausing for 60s before retrying."
            )
            time.sleep(60)
            restarts = 0
            last_restart_time = time.time()
            continue

        logger.info(f"Restarting in {RESTART_DELAY}s…")
        time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    run()
