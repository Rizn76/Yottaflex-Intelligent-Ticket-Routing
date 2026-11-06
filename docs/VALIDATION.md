# Validation and reproducibility

## Run

```powershell
py -m src.validate
py -m src.validate --data-dir data/small
```

The command reads normalized CSVs, the separate ground-truth directory, and `tickets.sqlite`. It writes `validation_report.json` and `validation_summary.txt` beside the generated data.

## Checks

- Primary-key and bridge-key uniqueness, CSV-to-SQLite schema/nullability agreement, required values, enumerated priority/status values, and timestamp ordering.
- Foreign-key integrity for every declared relationship in both CSV exports and SQLite `PRAGMA foreign_key_check`.
- Message-to-ticket channel/time consistency; routing skill/language compatibility; active-agent and concurrency limits.
- Customer-preferred ticket language, agent workload at the latest ticket-arrival snapshot, assignment interval/history, lifecycle event history/order, duplicate/revisited-agent transfer loops, and queue-overflow representation.
- SLA start/deadline recomputation from target and channel calendar, breach status/duration, and target reconciliation with expected priority/channel rules.
- Complaint safety priority, derived queue and intent, ground-truth consistency, manifest row counts, and each manifest SHA-256 checksum.
- With anomaly injection enabled, exactly one of each five known scenario labels is required and each must trigger its documented validator outcome. With `inject_anomalies` false, anomaly rows/labels must be absent.

## Expected versus unexpected

The five deliberately injected cases are reported in `expected_anomalies` with a `detected` flag. `ground_truth/anomalies.csv` and JSON include affected IDs, event references, expected and injected behavior, severity, and seed. They do not appear in `unexpected_errors` when recognized. In disabled mode, all expected ticket labels are `VALID` and no anomaly record is permitted. A malformed/missing anomaly or an unrelated integrity failure causes failure.

The reopened-history case intentionally lacks its original creation event; the priority case is intentionally below the safety minimum; the capacity case intentionally overlaps assignments. These controlled fixtures alter only their selected ticket/entity records. The validation report identifies them by deterministic anomaly ID and ticket ID rather than treating all injected labels as generic corruption. Operational ticket-event anomaly markers remain null; label files do not feed routing or SLA evaluation.

## Repeatability

```powershell
py -m src.reproducibility --seed 20261022 --tickets 100 --agents 10 --messages 250 --event-days 7
```

The script generates two independent project-local temporary directories, compares every table count and SHA-256 checksum, and cleans up its temporary outputs. For challenge-size verification, use a new output directory such as `data/phase5-final`; never target a retained dataset. Full generation with `--tickets 30000 --agents 100 --messages 100000 --event-days 90` requires more time and disk space. `src.analytics` reads that output and writes its summary beside it.

The manifest records the requested seed/scale, row counts, schema and runtime library versions, and SHA-256 checksums for exported files. The validator verifies manifest counts and checksums, CSV/SQLite consistency, key uniqueness and SQLite foreign keys, event/SLA rules, and the five expected anomaly detections. Import validation can be performed without a new file by executing `sql/seed_data.sql` against an in-memory SQLite connection and checking `PRAGMA foreign_key_check`.
