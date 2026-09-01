"""`make watch RUN_ID=x` — resolve outcomes for every link a run created.

Default mode uses webhooks already recorded in webhook_event (fast, but
depends on the cloudflared tunnel / webhook server having been up when the
customer paid). `--poll` (Makefile: POLL=1) uses the polling path instead —
GET /v1/payment_links/{id} for each link — which works even if the webhook
server was never running at all. Both paths call the same
src/attribute/rules.py::attribute(), so they agree on every episode given
the same underlying Razorpay state.

Prints which mode is active as the first line, per the phase spec.
"""

from __future__ import annotations

import typer

from src.attribute.ledger import compute_fp_cost, parse_outcome_assumptions, post_gross, post_net
from src.attribute.watcher import OutcomeWatcher
from src.config import load_settings, require_razorpay
from src.config_models import load_all
from src.db.migrate import get_connection
from src.db.repo import get_ledger_total
from src.razorpay_client import RazorpayClient


def main(
    run_id: str = typer.Option(..., "--run-id"),
    poll: bool = typer.Option(
        False, "--poll", help="Poll Payment Links instead of using webhooks."
    ),
    interval_s: int = typer.Option(20, "--interval-s"),
    timeout_s: int = typer.Option(300, "--timeout-s"),
) -> None:
    settings = load_settings()
    bundle = load_all()
    conn = get_connection(settings.db_path)
    watcher = OutcomeWatcher(window_hours=bundle.guardrails.attribution_window_hours)

    print(f"mode: {'polling' if poll else 'webhooks'}")

    try:
        if poll:
            key_id, key_secret = require_razorpay(settings)
            client = RazorpayClient(key_id, key_secret)
            try:
                attributions = watcher.by_polling(
                    conn, run_id, client, interval_s=interval_s, timeout_s=timeout_s
                )
            finally:
                client.close()
        else:
            attributions = watcher.from_webhooks(conn, run_id)

        for a in attributions:
            print(
                f"episode={a.episode_id} outcome={a.outcome} reason={a.reason_code} "
                f"recovered_paise={a.recovered_amount_paise}"
            )
            post_gross(conn, run_id, a)

        assumptions = parse_outcome_assumptions()
        fp = compute_fp_cost(conn, run_id, assumptions)
        net_paise = post_net(conn, run_id)
        gross_paise = get_ledger_total(conn, run_id, "gross_recovery")

        print(f"gross_paise={gross_paise}")
        print(f"fp_count={fp.fp_count} fp_cost_paise={fp.cost_paise}")
        print(f"net_paise={net_paise}")
    finally:
        conn.close()


if __name__ == "__main__":
    typer.run(main)
