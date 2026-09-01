# Exceptions sample — run `01M19J8M2AM3VAYX7SG3N447A7`

Every suppressed episode this run, grouped by reason_code, plus three
worked examples with the actual episode data. No episode is ever
silently dropped — see the accounting invariant in `src/runner.py`.

| reason_code | count |
|---|---|
| `duplicate_episode_this_run` | 5 |
| `episode_age_exceeds_cap` | 1 |

## Worked examples

- `pay_synthetic_00001` (upi, Rs 2875.24, error_reason='insufficient_fund') — **duplicate_episode_this_run**: duplicate check failed
- `pay_synthetic_00002` (upi, Rs 10124.95, error_reason='authentication_failed') — **duplicate_episode_this_run**: duplicate check failed
- `pay_synthetic_00003` (netbanking, Rs 904.96, error_reason='authentication_failed') — **duplicate_episode_this_run**: duplicate check failed
