# Exceptions sample — run `01M118P9RA2WM6ZMJQ60R61A5K`

Every suppressed episode this run, grouped by reason_code, plus three
worked examples with the actual episode data. No episode is ever
silently dropped — see the accounting invariant in `src/runner.py`.

| reason_code | count |
|---|---|
| `already_paid_elsewhere` | 2 |
| `quiet_hours_block` | 1 |
| `episode_age_exceeds_cap` | 1 |
| `customer_opted_out` | 1 |

## Worked examples

- `pay_synthetic_00002` (upi, Rs 10124.95, error_reason='authentication_failed') — **quiet_hours_block**: quiet_hours check failed
- `pay_synthetic_00003` (netbanking, Rs 904.96, error_reason='authentication_failed') — **already_paid_elsewhere**: terminal_seen check failed
- `pay_synthetic_00004` (card, Rs 1791.55, error_reason='card_number_invalid') — **already_paid_elsewhere**: terminal_seen check failed
