"""Rollback — cancel all Payment Links created in a run.

`make rollback RUN_ID=x` finds every execution in the run with status="created",
calls cancel_link on each, updates the status to "cancelled", and prints a
result table. Never swallows a failed cancel — exits non-zero if any link
could not be cancelled.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from rich.table import Table

from src.logging_setup import get_logger
from src.razorpay_client import RazorpayClient

IST = ZoneInfo("Asia/Kolkata")
logger = get_logger("rollback")


def rollback_run(
    conn: sqlite3.Connection,
    client: RazorpayClient,
    run_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Cancel all Payment Links created in a run.

    Returns:
        (successes, failures) where each is a list of {plink_id, short_url, result}
    """
    # Find all created links
    rows = conn.execute(
        "SELECT execution_id, plink_id, short_url FROM execution WHERE run_id = ? AND status = ?",
        (run_id, "created"),
    ).fetchall()

    if not rows:
        logger.info("no created links found for run %s", run_id)
        return [], []

    successes: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    now = datetime.now(IST).isoformat(timespec="seconds")

    for row in rows:
        execution_id = row["execution_id"]
        plink_id = row["plink_id"]
        short_url = row["short_url"]

        if not plink_id:
            logger.warning("execution %s has no plink_id", execution_id)
            failures.append(
                {"plink_id": plink_id, "short_url": short_url, "result": "no_plink_id"}
            )
            continue

        try:
            response = client.cancel_payment_link(plink_id)
            status = response.get("status", "unknown")

            if status == "cancelled":
                # Update status in database
                conn.execute(
                    "UPDATE execution SET status = ?, cancelled_at = ? WHERE execution_id = ?",
                    ("cancelled", now, execution_id),
                )
                conn.commit()
                logger.info("cancelled link %s", plink_id)
                successes.append(
                    {"plink_id": plink_id, "short_url": short_url, "result": "cancelled"}
                )
            else:
                # Already paid or some other non-cancellable state
                logger.warning("link %s returned status %s (not cancelled)", plink_id, status)
                failures.append({"plink_id": plink_id, "short_url": short_url, "result": status})

        except Exception as e:
            logger.error("failed to cancel link %s: %s", plink_id, e)
            failures.append({"plink_id": plink_id, "short_url": short_url, "result": f"error: {e}"})

    return successes, failures


def print_rollback_table(successes: list[dict[str, Any]], failures: list[dict[str, Any]]) -> None:
    """Print a formatted result table."""
    table = Table(title=f"Rollback Results: {len(successes)} succeeded, {len(failures)} failed")
    table.add_column("Payment Link ID", style="cyan")
    table.add_column("Short URL", style="magenta")
    table.add_column("Result", style="green")

    for item in successes:
        table.add_row(item["plink_id"], item.get("short_url", "—"), item["result"])

    for item in failures:
        table.add_row(
            item["plink_id"],
            item.get("short_url", "—"),
            f"[red]{item['result']}[/red]",
        )

    from rich.console import Console
    console = Console()
    console.print(table)


def main(argv: list[str]) -> int:
    """CLI entry point: `python -m src.execute.rollback --run-id <id>`"""
    import sys

    from src.config import load_settings, require_razorpay
    from src.db.migrate import get_connection

    if "--run-id" not in argv:
        print("Usage: python -m src.execute.rollback --run-id <run_id>")
        sys.exit(1)

    try:
        run_id_index = argv.index("--run-id")
        run_id = argv[run_id_index + 1]
    except (IndexError, ValueError):
        print("Error: --run-id requires a value")
        sys.exit(1)

    settings = load_settings()
    key_id, key_secret = require_razorpay(settings)

    conn = get_connection(settings.db_path)
    try:
        client = RazorpayClient(key_id, key_secret)
        try:
            successes, failures = rollback_run(conn, client, run_id)
            print_rollback_table(successes, failures)

            if failures:
                print(f"\n{len(failures)} link(s) could not be cancelled:")
                for f in failures:
                    print(f"  - {f['plink_id']}: {f['result']}")
                return 1

            print(f"\nSuccessfully cancelled {len(successes)} link(s)")
            return 0
        finally:
            client.close()
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
