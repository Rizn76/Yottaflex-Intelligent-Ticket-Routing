# Architecture

## Data flow

```text
seed + scale config
        |
        v
src/generator.py ----> valid baseline entities and event history
        |                         |
        |                         +--> five optional labeled anomaly fixtures
        v
CSV / JSON / SQLite / SQL seed + manifest and hashes
        |                         |
        |                         +--> ground_truth/ labels and anomaly references
        |
        +--> src/validate.py --> validation_report.json + summary
        +--> src/analytics.py --> analytics_summary.json
        +--> dashboard/app.py --> filtered operational views
```

## Modules

- `config.py`: challenge defaults and generation scale.
- `src/generator.py`: uses local seeded `random.Random` and NumPy `Generator` instances; creates parent records before children, builds valid event histories, optionally injects only the five named fixtures after baseline creation, and exports each format.
- `src/database.py`: opens SQLite connections with foreign keys enabled and initializes the schema.
- `sql/schema.sql`: normalized DDL, checks, primary/foreign keys, and query indexes.
- `src/routing.py`: explainable eligibility/ranking plus atomic SQLite ticket routing, priority dispatch, workload maintenance, fallback persistence, and idempotent SLA escalation.
- `src/sla_engine.py`: timezone-aware calendar arithmetic, effective-dated SLA policy lookup, and append-only persisted SLA cycles/breach snapshots.
- `src/lifecycle.py`: transition validation, append-only persistent events, assignment-capacity release, SLA completion/reopen integration, chronological checks, and transfer-loop detection.
- `src/validate.py`: integrity checks against generated CSVs and SQLite; enabled anomaly fixtures are reported as expected detections, while disabled-mode output must contain no labels.
- `src/reproducibility.py`: generates twice in project-local temporary directories and compares table counts and checksums.
- `src/analytics.py`: SQL relational reads plus Pandas summaries for ticket volume, queues, assignments, SLA cycles, workload, and separately reported injected/detected anomalies.
- `dashboard/app.py`: filtered Streamlit/Plotly operational views over SQLite, a ground-truth/validator comparison view, and explicit persistent dispatch/escalation controls.

## Relational design

Skills and languages are normalized into lookup/bridge relations; they are not stored as delimited lists in SQLite. Calendars have separate weekday and holiday relations. Tickets reference customer, channel, intent, language, and queue. Assignment, SLA, ticket event, and resolution rows are preserved as history rather than overwritten.

Ground truth is intentionally kept in a separate `ground_truth_labels` table and `ground_truth/` exports. It is not part of the ticket source feature CSVs. Detailed anomaly metadata (affected IDs, event references, expected/observed behavior, severity, and seed) is kept in `ground_truth/anomalies.csv` and JSON; only its schema-defined subset is loaded into the optional evaluation table.

## Routing decisions and assignment

An agent is eligible only when active, on shift in the agent's configured time zone, under concurrency limit, and skilled for the ticket's intent. Eligible agents are ranked by language match, utilization, workload, and stable ID. A non-critical case may use an explicit language-mismatch fallback; critical cases require an exact language match. Decisions include candidate rejection reasons, score, eligibility count, and fallback. An unassigned attempt is recorded with its explanation; a configured overflow queue is used when the queue is full.

`route_next_ticket` orders waiting work by critical/high/normal/low priority and then FIFO. `BEGIN IMMEDIATE` serializes dispatch and assignment writes so concurrent local SQLite callers cannot both consume the last capacity slot. Only `assigned` and `in_progress` tickets consume agent capacity; transition to open, pending, resolved, or closed releases it. `agents.current_workload` is maintained as a current snapshot from live assignment records.

## SLA calculations

`src/sla_engine.py` moves through local calendar days and consumes only minutes within each configured business window. It skips non-business weekdays and holiday dates. If opening and closing times are equal, the window represents a full 24-hour day (used for phone support). Naive input timestamps are interpreted in the provided calendar timezone; aware timestamps are converted to it. See `docs/BUSINESS_RULES.md`.

Database workflows select the effective priority/channel rule at clock start and append initial, completed, breached, and reopened-cycle SLA snapshots. Escalations are deduplicated by ticket, SLA type, and cycle; configured queue moves are capacity-checked, and the critical thresholds are five minutes before first-response due time and fifteen minutes before resolution due time.

## Lifecycle and anomaly fixtures

`open` is the queued state and `pending` represents waiting; no additional state is invented. The allowed transition graph and state-specific event types are defined in `src/lifecycle.py` and documented in `docs/BUSINESS_RULES.md`. Ticket events are append-only in SQLite. Reopen appends new SLA-cycle rows without deleting earlier history. Validation reconciles the final state event with the ticket's current status and checks assignment capacity/history.

Anomaly injection is a post-baseline generator option, enabled by default and disabled with `--no-anomalies`. The five categories are capacity overrun, channel-calendar SLA mismatch, missing creation history on one reopened ticket, transfer loop, and complaint-priority contradiction. IDs/reference metadata are seeded and labels are exported separately from operational records. Disabled mode has no anomaly rows or labels.

## Analytics and dashboard boundaries

`src/analytics.py` and dashboard read-only pages derive operational metrics from ticket, assignment, lifecycle, resolution, and latest SLA-cycle records. SLA compliance uses one latest snapshot per ticket/type/start-time cycle; dimensions are current ticket queue/channel/priority, and agent attribution uses the first recorded assignee. Transfer counts come from assignment rows marked `transferred`. Reopened cycles and due-soon clocks are reported separately. Injected anomaly labels are only read by the evaluation view and are compared with `validation_report.json`; they are not input to routing or SLA workflows. The dashboard's dispatch and escalation actions execute only after explicit button presses.

## Transaction and reproducibility notes

The generated SQLite file is rebuilt for each generation; the CSV/JSON artifacts are deterministic under the recorded versions and seed. Persistent routing and SLA workflow calls modify their SQLite database by design, so run them against a copy when retaining generated manifest checksums matters. Manifest hashes cover exported files except the manifest itself and post-generation validation/analytics reports. No wall-clock generation timestamp is included because it would make repeat runs differ.

## Interview explanation

“The generator builds valid normalized source records from one seed. Routing and SLA policies are separate functions so they can be tested independently. The database enforces relationships; validation then checks business rules that SQL constraints cannot express. Only after a valid baseline exists do I inject labeled edge cases, keeping ground truth out of feature files. Finally I verify repeatability through checksums and use SQL/Pandas for metrics.”
