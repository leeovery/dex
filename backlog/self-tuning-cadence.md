# Self-tuning cadence

A scheduled run can reschedule its own task
(`update_scheduled_task` is available to desktop scheduled sessions):
hourly while captures flow, daily when quiet. Precondition met
2026-08-20: first real run history exists (one instance, 17 hourly
runs — work clustered 20:05/23:05/09:05, nine consecutive overnight
no-ops; owner interest confirmed). Shape: simple backoff with hard
bounds — tighten after working runs, stretch after consecutive no-ops,
snap tight when captures arrive; report each reschedule with its reason.
Mostly a dex-run skill rule; queue behind the watchers design.
