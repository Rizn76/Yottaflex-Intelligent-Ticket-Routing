# Data dictionary

SQLite is the canonical relational store. CSV/JSON use the same normalized fields. Generated `TEXT` timestamps are ISO-8601; SQLite checks that timestamp values are parseable and enforces documented ordering where possible. IDs are immutable text keys. `INTEGER` boolean columns are constrained to `0`/`1`. A field marked optional may be SQL `NULL`.

## Master data and calendars

| Table.field | Type | Required | Key / allowed values | Meaning and validation |
|---|---|---:|---|---|
| `business_calendars.calendar_id` | TEXT | Yes | PK | Stable calendar identifier. |
| `business_calendars.calendar_name` | TEXT | Yes | UNIQUE | Human-readable calendar name. |
| `business_calendars.timezone` | TEXT | Yes | IANA zone | Zone used for shifts, SLA windows, and holidays. |
| `business_calendars.business_hours_start` | TEXT | Yes | `HH:MM` | Local opening time; equal to closing denotes a 24-hour day. |
| `business_calendars.business_hours_end` | TEXT | Yes | `HH:MM` | Local closing time; otherwise must be after opening. |
| `calendar_business_days.calendar_id` | TEXT | Yes | PK part, FK | Calendar whose weekday is available. |
| `calendar_business_days.weekday` | INTEGER | Yes | PK part, 0–6 (Mon–Sun) | Working weekday in Python's weekday numbering. |
| `calendar_holidays.calendar_id` | TEXT | Yes | PK part, FK | Calendar observing the holiday. |
| `calendar_holidays.holiday_date` | TEXT | Yes | PK part, ISO date | Date excluded from business-minute calculations. |
| `calendar_holidays.holiday_name` | TEXT | Yes | — | Holiday description. |
| `channels.channel_id` | TEXT | Yes | PK | Stable channel identifier. |
| `channels.channel_name` | TEXT | Yes | UNIQUE; email/chat/phone | Support contact channel. |
| `channels.business_hours_start/end` | TEXT | Yes | `HH:MM` | Duplicated calendar window for direct channel inspection. |
| `channels.timezone` | TEXT | Yes | IANA zone | Channel's calendar time zone; SQL requires it and the business hours to match the linked calendar, while Python validates the IANA name. |
| `channels.calendar_id` | TEXT | Yes | FK | Applicable business calendar. |
| `languages.language_id` | TEXT | Yes | PK | Stable language identifier. |
| `languages.language_name` | TEXT | Yes | UNIQUE | Display name used by tickets and agent-language bridges. |
| `intents.intent_id` | TEXT | Yes | PK | Stable support-intent identifier. |
| `intents.intent_name` | TEXT | Yes | UNIQUE | billing, technical_support, account_access, sales, refund, delivery, complaint. |
| `intents.default_priority` | TEXT | Yes | low/normal/high/critical | Default classification policy; complaint defaults to high. |
| `priority_rules.priority_rule_id` | TEXT | Yes | PK | Stable rule identifier. |
| `priority_rules.priority` | TEXT | Yes | low/normal/high/critical | Priority the rule applies to. |
| `priority_rules.channel_id` | TEXT | Yes | FK | Channel-specific rule scope. |
| `priority_rules.first_response_minutes` | INTEGER | Yes | > 0 | Business minutes allowed to first response. |
| `priority_rules.resolution_minutes` | INTEGER | Yes | > 0 | Business minutes allowed to resolution. |
| `priority_rules.effective_from` | TEXT | Yes | ISO timestamp | Inclusive rule start. |
| `priority_rules.effective_to` | TEXT | No | ISO timestamp > start | Exclusive rule end; `NULL` means current. |

## Customers, agents, skills, queues

| Table.field | Type | Required | Key / allowed values | Meaning and validation |
|---|---|---:|---|---|
| `customers.customer_id` | TEXT | Yes | PK | `CUS-` plus zero-padded sequence. |
| `customers.customer_segment` | TEXT | Yes | consumer/small_business/enterprise | Synthetic customer segment. |
| `customers.preferred_language_id` | TEXT | No | FK to languages | Optional customer language preference. |
| `customers.country` | TEXT | Yes | Two-letter sample code | Synthetic customer locale. |
| `customers.created_at` | TEXT | Yes | ISO timestamp | Customer record creation. |
| `agents.agent_id` | TEXT | Yes | PK; `AGT-000001` pattern | Stable agent identifier. |
| `agents.agent_name` | TEXT | Yes | — | Synthetic display name. |
| `agents.timezone` | TEXT | Yes | IANA zone | Used to evaluate local shift schedule. |
| `agents.shift_start/end` | TEXT | Yes | `HH:MM` | Local shift; end before start represents overnight shift. |
| `agents.active` | INTEGER | Yes | 0/1 | Whether the agent may receive a new assignment. |
| `agents.max_concurrency` | INTEGER | Yes | > 0 | Maximum simultaneous active assignments. |
| `agents.current_workload` | INTEGER | Yes | >= 0 | Current snapshot maintained by persistent routing/lifecycle operations; only live assignments for tickets in `assigned` or `in_progress` consume capacity. |
| `skills.skill_id` | TEXT | Yes | PK | Stable skill identifier. |
| `skills.skill_name` | TEXT | Yes | UNIQUE | Skill taxonomy; aligned with intent names in this sample. |
| `agent_skills.agent_id` | TEXT | Yes | Composite PK; FK | Agent participating in this skill. |
| `agent_skills.skill_id` | TEXT | Yes | Composite PK; FK | Skill assigned to the agent. |
| `agent_languages.agent_id` | TEXT | Yes | Composite PK; FK | Agent participating in this language. |
| `agent_languages.language_id` | TEXT | Yes | Composite PK; FK | Language assigned to the agent. |
| `queues.queue_id` | TEXT | Yes | PK | Stable queue identifier. |
| `queues.queue_name` | TEXT | Yes | UNIQUE | Human-readable intent-aligned queue name. |
| `queues.intent_id` | TEXT | Yes | FK | Queue's primary intent. |
| `queues.capacity` | INTEGER | Yes | > 0 | Maximum concurrent in-queue backlog. |
| `queues.active` | INTEGER | Yes | 0/1 | Whether the queue accepts new work. |

## Tickets and operational history

| Table.field | Type | Required | Key / allowed values | Meaning and validation |
|---|---|---:|---|---|
| `tickets.ticket_id` | TEXT | Yes | PK; `TKT-2026-000001` | Stable ticket identifier. |
| `tickets.customer_id` | TEXT | Yes | FK | Ticket owner. |
| `tickets.channel_id` | TEXT | Yes | FK | Channel of origin. |
| `tickets.intent_id` | TEXT | Yes | FK | Ticket intent. |
| `tickets.language_id` | TEXT | Yes | FK | Customer's language for this ticket. |
| `tickets.priority` | TEXT | Yes | low/normal/high/critical | Current priority; complaint policy minimum is high. |
| `tickets.queue_id` | TEXT | Yes | FK | Current intent-aligned queue. |
| `tickets.status` | TEXT | Yes | created/open/assigned/in_progress/pending/resolved/closed | Current lifecycle state. |
| `tickets.subject` | TEXT | Yes | — | Synthetic subject text. |
| `tickets.created_at` | TEXT | Yes | ISO timestamp | Ticket creation time. |
| `tickets.updated_at` | TEXT | Yes | ISO timestamp >= created_at | Current ticket update time. |
| `messages.message_id` | TEXT | Yes | PK; per-ticket sequence | `MSG-<ticket-id>-001`. |
| `messages.ticket_id` | TEXT | Yes | FK | Parent ticket. |
| `messages.channel_id` | TEXT | Yes | FK | Channel used for the message; must match its ticket. |
| `messages.sender_type` | TEXT | Yes | customer/agent/system | Message sender class. |
| `messages.body` | TEXT | Yes | — | Synthetic message body; no real customer content. |
| `messages.created_at` | TEXT | Yes | ISO timestamp >= ticket creation | Message timestamp. |
| `assignment_events.assignment_id` | TEXT | Yes | PK; `ASN-<ticket-id>-001` | Immutable assignment record ID. |
| `assignment_events.ticket_id` | TEXT | Yes | FK | Ticket being assigned. |
| `assignment_events.agent_id` | TEXT | No | FK | Assigned agent; null only for overflow. |
| `assignment_events.queue_id` | TEXT | Yes | FK | Queue handling the assignment. |
| `assignment_events.assigned_at` | TEXT | Yes | ISO timestamp | Assignment start. |
| `assignment_events.unassigned_at` | TEXT | No | ISO timestamp >= assigned_at | Assignment end; null means still active. |
| `assignment_events.assignment_status` | TEXT | Yes | assigned/transferred/unassigned/overflow | Assignment outcome; `overflow` also records an unassigned queue fallback attempt. |
| `assignment_events.decision_reason` | TEXT | Yes | — | Generator records a human-readable reason; persistent routing records deterministic decision JSON with agent/queue/time, priority, skill/language, score, eligibility count, rejection reasons, and fallback. |
| `assignment_events.assignment_number` | INTEGER | Yes | > 0; unique per ticket | Per-ticket assignment sequence. |
| `sla_events.sla_id` | TEXT | Yes | PK | Stable unique SLA event ID; generated as `SLA-<ticket-id>` for resolution and `-FR` suffix for first response. |
| `sla_events.ticket_id` | TEXT | Yes | FK | Ticket governed by this SLA. |
| `sla_events.sla_type` | TEXT | Yes | first_response/resolution | SLA clock purpose; a ticket can have multiple records of the same type across lifecycle cycles. |
| `sla_events.target_minutes` | INTEGER | Yes | > 0 | Business-minute target. |
| `sla_events.started_at` | TEXT | Yes | ISO timestamp | SLA clock start. |
| `sla_events.due_at` | TEXT | Yes | ISO timestamp | Calendar-derived deadline. |
| `sla_events.actual_at` | TEXT | No | ISO timestamp | Actual response/resolution; null while pending. |
| `sla_events.breached` | INTEGER | Yes | 0/1 | Whether the actual/matured target breached. |
| `sla_events.breach_minutes` | INTEGER | Yes | >= 0 | Business minutes beyond the deadline at completion, or at first persisted overdue evaluation for a pending clock. |
| `sla_events.escalation_at` | TEXT | No | ISO timestamp | Escalation deadline for critical SLA. |
| `sla_events.calendar_id` | TEXT | Yes | FK | Required calendar used in the calculation; SQL requires it to match the ticket channel's calendar. |
| `sla_events` history | — | — | Append-only | Updates and deletes are rejected; record a new row for a recalculated or reopened SLA. Ordered by `started_at`, then `sla_id` for ties. |
| `ticket_events.event_id` | TEXT | Yes | PK | Stable event ID. |
| `ticket_events.ticket_id` | TEXT | Yes | FK | Ticket whose history is recorded. |
| `ticket_events.event_type` | TEXT | Yes | created/opened/assigned/work_started/state_changed/pending/transferred/escalated/reopened/resolved/closed/capacity_violation | Event category emitted by the generator or lifecycle helper; state transitions use the corresponding state-specific event. |
| `ticket_events.from_state/to_state` | TEXT | No | created/open/assigned/in_progress/pending/resolved/closed | Optional state endpoints; non-null values are constrained by SQL. Legal transition pairs and chronological stream consistency are application-level validation. |
| `ticket_events.actor_id` | TEXT | No | FK to agents | Agent performing/associated with the event. |
| `ticket_events.event_at` | TEXT | Yes | Parseable timestamp | Event time; per-ticket chronology is checked by the application validator. |
| `ticket_events.details` | TEXT | Yes | — | Event explanation. |
| `ticket_events.anomaly_type` | TEXT | No | — | Reserved optional marker; generated evaluation labels are kept in separate ground-truth files and this operational column remains null. |
| `ticket_events` history | — | — | Append-only; SQL update/delete triggers | Existing lifecycle events cannot be rewritten or deleted. |
| `resolutions.resolution_id` | TEXT | Yes | PK | Stable resolution record ID. |
| `resolutions.ticket_id` | TEXT | Yes | FK | Resolved ticket; multiple historical resolutions are allowed. |
| `resolutions.resolved_at` | TEXT | No | ISO timestamp | Resolution time; null if unresolved. |
| `resolutions.resolution_code` | TEXT | Yes | — | Outcome category or `unresolved`. |
| `resolutions.resolved` | INTEGER | Yes | 0/1 | Resolution outcome. |
| `resolutions.resolution_minutes` | INTEGER | No | >= 0 | Business minutes consumed. |
| `resolutions.csat_score` | INTEGER | No | 1–5 | Synthetic satisfaction score. |

## Evaluation-only records

These are exported separately from source feature files and must not be used as model input.

| Table.field | Type | Required | Key / allowed values | Meaning and validation |
|---|---|---:|---|---|
| `anomalies.anomaly_id` | TEXT | Yes | PK; deterministic `ANM-` ID | Stable scenario label. |
| `anomalies.ticket_id` | TEXT | Yes | FK; UNIQUE | Ticket containing an intentional edge case; at most one anomaly row per ticket. |
| `anomalies.anomaly_type` | TEXT | Yes | agent_over_capacity/sla_ignores_channel_calendar/reopen_loses_history/transfer_loop/priority_safety_contradiction | Anomaly class; constrained by SQL. |
| `anomalies.description` | TEXT | Yes | — | Human-readable injected condition. |
| `anomalies.expected_validation` | TEXT | Yes | Expected check code | Validator result expected for the scenario. |
| `anomalies.ground_truth_label` | TEXT | Yes | `expected_detection` | Evaluation label, never a feature. |
| `ground_truth/anomalies.csv` `affected_entity_ids` | JSON TEXT | Yes for labeled rows | JSON object | IDs for affected tickets, agents, queues, SLAs, or resolutions. |
| `ground_truth/anomalies.csv` `event_references` | JSON TEXT | Yes for labeled rows | JSON object of ID arrays | Related `ticket_event_ids`, `assignment_ids`, and `sla_ids`; the missing creation event is noted by `missing_event_type` in affected entities. |
| `ground_truth/anomalies.csv` `reference_timestamp` | ISO timestamp | Yes for labeled rows | — | Timestamp anchoring the injected condition. |
| `ground_truth/anomalies.csv` `expected_behavior` | TEXT | Yes for labeled rows | — | Correct behavior expected before injection. |
| `ground_truth/anomalies.csv` `observed_behavior` | TEXT | Yes for labeled rows | — | Inconsistent behavior injected for validation. |
| `ground_truth/anomalies.csv` `severity` | TEXT | Yes for labeled rows | low/medium/high | Evaluation severity classification. |
| `ground_truth/anomalies.csv` `seed` | INTEGER | Yes for labeled rows | Manifest seed | Deterministic generation seed for the fixture. |
| `ground_truth_labels.ticket_id` | TEXT | Yes | PK and FK | One evaluation label per ticket. |
| `ground_truth_labels.expected_intent` | TEXT | Yes | Intent taxonomy | Rule-derived intent label. |
| `ground_truth_labels.expected_priority` | TEXT | Yes | low/normal/high/critical | Rule-derived priority, including complaint safety minimum. |
| `ground_truth_labels.expected_queue_id` | TEXT | Yes | Queue FK | Intent-aligned expected queue. |
| `ground_truth_labels.expected_routing_outcome` | TEXT | Yes | assigned/queue_overflow | Outcome predicted by the documented routing policy. |
| `ground_truth_labels.expected_agent_id` | TEXT | No | FK to agents | Expected initial agent; null for an overflow decision. |
| `ground_truth_labels.expected_decision_reason` | TEXT | Yes | — | Explainable reason recorded by the initial routing policy. |
| `ground_truth_labels.expected_sla_outcome` | TEXT | Yes | met/breached | Outcome derived from the computed SLA and actual timestamp. |
| `ground_truth_labels.anomaly_type` | TEXT | No | Five scenario types | Null for baseline ticket; set only for injected scenario tickets. |
| `ground_truth_labels.expected_validation` | TEXT | Yes | VALID or check code | Expected validation outcome. |

## Constraint ownership

- **SQL constraints and triggers:** primary/foreign keys; required fields; enum and numeric bounds; timestamp parseability and ticket/SLA time ordering; one anomaly per ticket; channel/calendar consistency; SLA/calendar consistency; message time not before ticket creation; message channel matching its ticket; and immutable SLA/ticket-event history.
- **Application validation:** IANA timezone existence, lifecycle transition legality and event chronology, priority-rule overlap, complaint safety priority, routing/queue consistency, and derived SLA calendar correctness. These cross-record/business rules are reported by `src/validate.py` because SQLite `CHECK` constraints cannot query other rows or calculate business calendars.
- Schema version 2 is applied by `src/database.py`. Initialization is repeatable; the versioned migration rebuilds the affected SLA, ticket-event, and anomaly tables while preserving their rows, installs null/empty primary-key guards for legacy text-key tables, then checks legacy timestamp, calendar, message, and foreign-key consistency.
- Generator configuration records `inject_anomalies` in `manifest.json`; injection is enabled by default and disabled with `--no-anomalies`. Detailed anomaly labels remain separate and never enter routing inputs.

## Analytics output

`src.analytics.summarize` writes `analytics_summary.json` beside the selected dataset. It includes ticket counts and UTC creation-day volume, current queue backlog, unassigned/reopened/transfer metrics, agent workload and distinct assigned/resolved ticket counts, completed SLA duration averages by first assignee, and SLA metrics.

SLA counts use the latest immutable record per `(ticket_id, sla_type, started_at)` cycle. Compliance is `1 - breach_count / cycle_count`; the denominator includes pending cycles with a persisted not-breached snapshot. Duration averages include completed cycles only and use business minutes. Per-agent SLA attribution uses the first recorded assignee; queue, channel, and priority are current ticket attributes. An SLA approaches its deadline when it is incomplete, not breached, and due within the next 24 hours at analysis time.

Transfer totals count `assignment_events` rows whose status is `transferred`; per-ticket counts include each such assignment-history row.

The `anomaly_ground_truth_vs_detected` section compares separately stored injected labels with expected-case detection rows in `validation_report.json`. It does not report precision/recall/F1 because no complete negative/false-positive population is defined. The summary is an analytics artifact, not an operational input.
