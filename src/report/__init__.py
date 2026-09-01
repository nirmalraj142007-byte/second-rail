"""evidence/report.md rendering — scripts/eval.py's only consumer.

Three submodules, one job each:
  render.py       the data contract (ReportData and friends) + the fixed
                   section order + the honesty guards (no bare point
                   estimate, no unqualified rupee figure).
  sensitivity.py   the +/-30% sweep over exactly three outcome_model.md
                   parameters.
  charts.py        matplotlib -> evidence/charts/*.png, committed.

Nothing in this package touches an LLM, a gate check, or the sealed
labels file directly — it only renders what scripts/eval.py already
computed and handed it.
"""
