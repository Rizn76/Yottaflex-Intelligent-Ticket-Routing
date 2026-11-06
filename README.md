# YottaFlex AI — Support Ticket Routing & SLA Challenge

An executable synthetic customer-support operations simulator for email, chat, and phone tickets. It addresses the operational problem of routing work to qualified agents without exceeding capacity, while measuring queue backlog, lifecycle outcomes, and channel-calendar SLA performance. The generated data is synthetic and designed for reproducible evaluation, not production use.

## What is implemented

- Deterministic generation with seed `20261022`, adjustable scale, immutable zero-padded IDs, and a versioned manifest.
- Normalized SQLite schema with foreign keys, bridge tables for agent skills/languages, and calendar tables.
- Deterministic, explainable routing with required skill, language, shift, live workload, concurrency, queue capacity, priority ordering, and recorded fallback reasons.
- Persisted assignment and workload updates in serialized SQLite transactions; repeated live assignment attempts are rejected.
- Effective-dated, channel-specific first-response and resolution SLA cycles, immutable breach/completion history, and idempotent critical-ticket escalation.
- Append-only ticket lifecycle history with state-specific events, preserved reassignment history, capacity release, and new SLA clocks on reopen.
- Five deterministic, explicitly labeled anomaly fixtures, enabled by default and disabled with `--no-anomalies`. Detailed labels live under `ground_truth/`, separate from operational records.
- CSV, JSON, SQLite, and SQL seed exports; manifest row counts and SHA-256 checksums.
- Validation reports distinguish the five expected anomalies from unexpected data errors.
- SQL/pandas operational metrics and an eight-page Streamlit/Plotly dashboard.

## Windows / VS Code setup

Use Python 3.11 or newer and open this folder in VS Code. In PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install --upgrade pip
py -m pip install -r requirements.txt
```

If PowerShell blocks activation, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that terminal and activate again.
`tzdata` is included so Python's `zoneinfo` can resolve IANA timezones on Windows installations without an operating-system timezone database.

## Configuration and database initialization

Defaults are centralized in the project-root `config.py`. Override them per PowerShell session with `YOTTAFLEX_*` environment variables; command-line arguments override environment values:

With no override, the generator and dashboard use `data/generated`; the dashboard opens `data/generated/tickets.sqlite`. A generator `--output-dir` option selects that run's destination but does not change the dashboard's configured dataset. To view a dataset generated elsewhere, set `YOTTAFLEX_OUTPUT_DIR` to that same directory before launching the dashboard.

```powershell
$env:YOTTAFLEX_TICKETS = "100"
$env:YOTTAFLEX_AGENTS = "10"
$env:YOTTAFLEX_MESSAGES = "250"
$env:YOTTAFLEX_EVENT_DAYS = "7"
$env:YOTTAFLEX_OUTPUT_DIR = "data/phase4-small"
$env:YOTTAFLEX_TIMEZONE = "Asia/Kolkata"
py -m src.generator
```

Supported settings are `SEED`, `TICKETS`, `AGENTS`, `MESSAGES`, `EVENT_DAYS`, `START_DATE`, `OUTPUT_DIR`, and `TIMEZONE`, all prefixed with `YOTTAFLEX_`. `YOTTAFLEX_DATA_DIR` is accepted as an alias for `YOTTAFLEX_OUTPUT_DIR`.

Initialize or upgrade the SQLite schema without regenerating data:

```powershell
py -m src.database --database data/schema-check.sqlite
```

Initialization enables foreign keys, applies schema version 2 idempotently, and preserves existing history rows during migration. Foreign-key violations cause initialization to fail explicitly.
Do not upgrade a generated dataset's SQLite file in place if its manifest checksums must remain valid; regenerate that dataset with `src.generator` to export a matching database and manifest.

## Persistent routing workflows

The routing module can be used with any initialized schema-version-2 database. `route_next_ticket` dispatches the highest-priority open ticket from a queue; `route_ticket_in_database` routes a specific ticket. Both return the decision explanation and persist assignment, ticket-state, workload, and initial SLA history atomically. `process_sla_escalations` can be called by a local scheduler to record due escalations and optionally move tickets to an active escalation queue. `transition_ticket_in_database` records lifecycle changes, releases agent capacity, closes resolution SLAs, and starts new SLA clocks on reopen.

These are local SQLite workflows, not a distributed production service. Use `src.database.connect` so foreign-key enforcement is enabled.

## Generate, validate, and analyze

**Warning:** Generation replaces files inside the selected output directory; it does not merge with existing files. Check that the destination does not already contain data you need to keep. Never target a retained dataset such as `data/generated` or `data/phase5-final`; choose a fresh output directory for each run. The example paths below are ignored by Git.

```powershell
py -m src.generator --seed 20261022 --tickets 30000 --agents 100 --messages 100000 --event-days 90 --output-dir data/phase5-new-run
py -m src.validate --data-dir data/phase5-new-run
py -m src.analytics --data-dir data/phase5-new-run
```

To open that full-scale dataset in the dashboard, select the same path:

```powershell
$env:YOTTAFLEX_OUTPUT_DIR = "data/phase5-new-run"
py -m streamlit run dashboard/app.py
```

Generate a small development dataset:

```powershell
py -m src.generator --seed 20261022 --tickets 100 --agents 10 --messages 250 --event-days 7 --output-dir data/phase4-small
py -m src.validate --data-dir data/phase4-small
py -m src.analytics --data-dir data/phase4-small
```

To open the small dataset in the dashboard, select its path instead:

```powershell
$env:YOTTAFLEX_OUTPUT_DIR = "data/phase4-small"
py -m streamlit run dashboard/app.py
```

The generator injects exactly one fixture for each of the five documented anomaly categories by default. It requires at least five tickets and three agents in that mode. Use `--no-anomalies` to generate valid baseline-only data (at least one ticket and agent) with no anomaly labels. It rejects invalid scale values instead of silently generating partial output.

Outputs include normalized CSVs, `json/`, `tickets.sqlite`, `sql/seed_data.sql`, `ground_truth/ground_truth.csv`, detailed `ground_truth/anomalies.csv` and JSON, and `manifest.json`. Anomaly labels include affected IDs, referenced events, timestamp, expected/observed behavior, severity, and seed. Validation writes `validation_report.json` and `validation_summary.txt`; analytics writes `analytics_summary.json`.

Analytics definitions:

- Ticket volume is counted by creation date/channel/intent/priority/status; queue backlog is the current count in `open`, `assigned`, `in_progress`, or `pending`.
- Unassigned tickets are open/pending tickets without a live, non-null agent assignment. Reopens count distinct tickets with a `reopened` lifecycle event; transfers count assignment-history rows marked `transferred`.
- Agent assigned/resolved counts are distinct ticket IDs from assignment history and `resolved` lifecycle events attributed to that agent. Per-agent first-response/resolution averages use completed business-calendar SLA cycles attributed to the first assignee. Peak utilization is maximum historical concurrent assignment count divided by configured concurrency. Workload imbalance is maximum assigned tickets per agent divided by the mean across all agents.
- SLA analytics use the latest immutable snapshot per ticket, SLA type, and start time (one SLA cycle). Compliance denominator is the number of such cycles; breach duration is the stored business-calendar duration. Average response/resolution durations use completed cycles and business minutes.
- SLA-by-agent association uses the ticket's first recorded assignee; queue, channel, and priority are the ticket's current attributes. This is attribution, not proof that the assignee performed every SLA action.
- “Approaching” means an incomplete, not-yet-breached SLA with a due time within the next 24 hours at analysis time. Reopened cycles are identified by more than one pair of first-response/resolution cycles on a ticket.
- Injected ground truth is compared separately with the validator's expected-anomaly detections. Precision/recall/F1 are intentionally omitted because the validator report does not define a complete negative/false-positive population.

Repeat generation in two temporary directories and compare counts/checksums:

```powershell
py -m src.reproducibility --seed 20261022 --tickets 100 --agents 10 --messages 250 --event-days 7
```

## Dashboard

By default, the dashboard reads `data/generated/tickets.sqlite`. To use another dataset, first generate it and select the same directory with `YOTTAFLEX_OUTPUT_DIR` as shown in the full-scale and small-dataset examples above.

```powershell
py -m streamlit run dashboard/app.py
```

Dashboard pages: Overview, Ticket Analytics, Agent Performance, Queue Management, SLA Monitoring, Routing Decisions, Anomaly Detection, and Data Validation. Read-only views support date, channel, priority, queue, and status filters. The Routing Decisions page provides explicit buttons for dispatching the next ticket and processing due SLA escalations; these actions modify the selected SQLite database and do not run on page load. Ground-truth labels are displayed only in the evaluation page and are not passed into routing or SLA workflows.

## Tests

```powershell
py -m pytest -q
```

Tests use small temporary datasets. The full challenge-size generation and validation are performed separately before submission, using a distinct output directory.

## Phase 5 verification

On Windows with the recorded environment, the separate `data/phase5-final` output was generated with seed `20261022` in 52.9 seconds. It contains exactly 30,000 tickets, 100 agents, 100,000 messages, 156,190 ticket events, 60,000 SLA events, 30,004 assignment events, and exactly five anomaly fixtures. Ticket creation dates cover 90 distinct UTC dates from 2026-08-01 through 2026-10-29.

The full-scale validator passed with zero unexpected errors and detected all five expected anomaly categories. SQLite reported `integrity_check = ok` and zero foreign-key violations. The exported SQL seed imported into an in-memory SQLite database with 30,000 tickets and no foreign-key violations. Analytics returned 30,000 tickets and matched each of the five injected anomaly types to one validator detection.

The final test run reported **48 passed**; Python compilation passed; `pip check` found no broken requirements. Seeded reproducibility was previously verified at both 100 tickets/10 agents/250 messages/7 days and full challenge scale. The full-scale repeat took 2 minutes 10 seconds and matched counts plus checksums across 44 output files. All eight dashboard pages rendered against the small sample, and the full-scale Overview rendered without exceptions. Navigation including the routing page did not change the database; its two write actions remain explicit, unclicked buttons.

The latest read-only checksum verification confirmed that all 44 manifest-listed files in `data/phase5-final` match. This checked the existing files against their manifest; it did not independently regenerate the dataset. An earlier report said all 44 checksums in `data/generated` were unchanged; that was later corrected: 43 entries match, while `tickets.sqlite` has a checksum mismatch. The current database has two additional routing-related records relative to its earlier validation snapshot, but its SQLite integrity check is `ok` and it has zero foreign-key violations. Its prior validation report describes the earlier snapshot, so `data/generated` is not the checksum-verified baseline.

## Architecture and documentation

- [Architecture](docs/ARCHITECTURE.md): module boundaries, data flow, and design decisions.
- [ER diagram](docs/ER_DIAGRAM.md): cardinality, bridge tables, and optional relationships.
- [Data dictionary](docs/DATA_DICTIONARY.md): fields, types, key/optional semantics, and validation rules.
- [Business rules](docs/BUSINESS_RULES.md): priority, routing, SLA, lifecycle, and label rules.
- [Assumptions](docs/ASSUMPTIONS.md): synthetic environment and known constraints.
- [Validation](docs/VALIDATION.md): checks, expected anomaly behavior, and report interpretation.
- [Demo script](docs/DEMO_SCRIPT.md): short interview walkthrough.
- [AI prompt history](AI_PROMPT_LOG.md): truthful record of AI assistance and actual verification.
- [Analytics SQL](sql/analytics_queries.sql): reusable operational queries.

## Data integrity and safety

All people, organizations, tickets, messages, and outcomes are synthetic. Do not replace these generated records with private customer information. Keep generated datasets and database files out of public GitHub; the `.gitignore` excludes the normal output folders and database files.

## Design choices and limitations

- SQLite and Python's standard `sqlite3` module keep local setup simple; Pandas/NumPy support data generation and analysis.
- Calendar rules in the generated sample use Asia/Kolkata, weekday schedules, and explicitly synthetic holidays. Replace them with approved business calendars before using any real operational data.
- Routing and SLA workflows persist locally and serialize competing SQLite writers. SQLite locking is suitable for the demonstration; this is not a distributed production queue or authenticated service.
- The first three synthetic agents are intentionally configured as fixture profiles to make capacity and transfer edge cases reproducible at small scales.
- Generated SLA target/outcome labels follow the rules documented in `docs/BUSINESS_RULES.md`; they are not independent random labels.
- Runtime and artifact size depend on Python/Pandas/NumPy versions and selected scale. The manifest records versions, configuration, counts, and checksums; identical bytes are expected within the same recorded environment.
- The Streamlit dashboard is a local demonstration, not an authenticated or production-hardened application.

## Submission readiness

See [SUBMISSION_CHECKLIST.md](docs/SUBMISSION_CHECKLIST.md) for deliverables and publication checks. No repository creation, staging, or publishing is performed by these project commands.
