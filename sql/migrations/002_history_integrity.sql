BEGIN IMMEDIATE;

CREATE TABLE sla_events__new (
    sla_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(sla_id)) > 0),
    ticket_id TEXT NOT NULL,
    sla_type TEXT NOT NULL CHECK (sla_type IN ('first_response', 'resolution')),
    target_minutes INTEGER NOT NULL CHECK (target_minutes > 0),
    started_at TEXT NOT NULL,
    due_at TEXT NOT NULL,
    actual_at TEXT,
    breached INTEGER NOT NULL CHECK (breached IN (0, 1)),
    breach_minutes INTEGER NOT NULL DEFAULT 0 CHECK (breach_minutes >= 0),
    escalation_at TEXT,
    calendar_id TEXT NOT NULL,
    CHECK (julianday(started_at) IS NOT NULL),
    CHECK (julianday(due_at) IS NOT NULL),
    CHECK (julianday(due_at) >= julianday(started_at)),
    CHECK (actual_at IS NULL OR julianday(actual_at) IS NOT NULL),
    CHECK (actual_at IS NULL OR julianday(actual_at) >= julianday(started_at)),
    CHECK (escalation_at IS NULL OR julianday(escalation_at) IS NOT NULL),
    CHECK (escalation_at IS NULL OR julianday(escalation_at) >= julianday(started_at)),
    FOREIGN KEY (ticket_id) REFERENCES tickets(ticket_id),
    FOREIGN KEY (calendar_id) REFERENCES business_calendars(calendar_id)
);

INSERT INTO sla_events__new (
    sla_id, ticket_id, sla_type, target_minutes, started_at, due_at, actual_at,
    breached, breach_minutes, escalation_at, calendar_id
)
SELECT
    sla_id, ticket_id, sla_type, target_minutes, started_at, due_at, actual_at,
    breached, breach_minutes, escalation_at, calendar_id
FROM sla_events;

DROP TABLE sla_events;
ALTER TABLE sla_events__new RENAME TO sla_events;

CREATE TABLE ticket_events__new (
    event_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(event_id)) > 0),
    ticket_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN (
        'created', 'opened', 'assigned', 'work_started', 'state_changed', 'pending', 'transferred',
        'escalated', 'reopened', 'resolved', 'closed', 'capacity_violation'
    )),
    from_state TEXT CHECK (from_state IS NULL OR from_state IN (
        'created', 'open', 'assigned', 'in_progress', 'pending', 'resolved', 'closed'
    )),
    to_state TEXT CHECK (to_state IS NULL OR to_state IN (
        'created', 'open', 'assigned', 'in_progress', 'pending', 'resolved', 'closed'
    )),
    actor_id TEXT,
    event_at TEXT NOT NULL CHECK (julianday(event_at) IS NOT NULL),
    details TEXT NOT NULL CHECK (length(trim(details)) > 0),
    anomaly_type TEXT,
    CHECK (
        (event_type = 'created' AND from_state IS NULL AND to_state = 'created')
        OR (event_type = 'escalated' AND from_state IS NULL AND to_state IS NULL)
        OR (
            event_type NOT IN ('created', 'escalated')
            AND from_state IS NOT NULL
            AND to_state IS NOT NULL
        )
    ),
    FOREIGN KEY (ticket_id) REFERENCES tickets(ticket_id) ON DELETE CASCADE,
    FOREIGN KEY (actor_id) REFERENCES agents(agent_id)
);

INSERT INTO ticket_events__new (
    event_id, ticket_id, event_type, from_state, to_state, actor_id, event_at,
    details, anomaly_type
)
SELECT
    event_id, ticket_id, event_type, from_state, to_state, actor_id, event_at,
    details, anomaly_type
FROM ticket_events;

DROP TABLE ticket_events;
ALTER TABLE ticket_events__new RENAME TO ticket_events;

CREATE TABLE anomalies__new (
    anomaly_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(anomaly_id)) > 0),
    ticket_id TEXT NOT NULL,
    anomaly_type TEXT NOT NULL CHECK (anomaly_type IN (
        'agent_over_capacity', 'sla_ignores_channel_calendar', 'reopen_loses_history',
        'transfer_loop', 'priority_safety_contradiction'
    )),
    description TEXT NOT NULL CHECK (length(trim(description)) > 0),
    expected_validation TEXT NOT NULL CHECK (length(trim(expected_validation)) > 0),
    ground_truth_label TEXT NOT NULL CHECK (length(trim(ground_truth_label)) > 0),
    FOREIGN KEY (ticket_id) REFERENCES tickets(ticket_id)
);

INSERT INTO anomalies__new (
    anomaly_id, ticket_id, anomaly_type, description, expected_validation, ground_truth_label
)
SELECT
    anomaly_id, ticket_id, anomaly_type, description, expected_validation, ground_truth_label
FROM anomalies;

DROP TABLE anomalies;
ALTER TABLE anomalies__new RENAME TO anomalies;

CREATE UNIQUE INDEX IF NOT EXISTS uq_anomalies_ticket_id ON anomalies(ticket_id);

COMMIT;
