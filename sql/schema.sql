PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS business_calendars (
    calendar_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(calendar_id)) > 0),
    calendar_name TEXT NOT NULL UNIQUE,
    timezone TEXT NOT NULL,
    business_hours_start TEXT NOT NULL,
    business_hours_end TEXT NOT NULL,
    CHECK (length(trim(calendar_name)) > 0),
    CHECK (length(trim(timezone)) > 0),
    CHECK (time(business_hours_start) IS NOT NULL),
    CHECK (time(business_hours_end) IS NOT NULL),
    CHECK (business_hours_start <= business_hours_end)
);

CREATE TABLE IF NOT EXISTS calendar_business_days (
    calendar_id TEXT NOT NULL CHECK (length(trim(calendar_id)) > 0),
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    PRIMARY KEY (calendar_id, weekday),
    FOREIGN KEY (calendar_id) REFERENCES business_calendars(calendar_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS calendar_holidays (
    calendar_id TEXT NOT NULL CHECK (length(trim(calendar_id)) > 0),
    holiday_date TEXT NOT NULL,
    holiday_name TEXT NOT NULL,
    PRIMARY KEY (calendar_id, holiday_date),
    CHECK (date(holiday_date) IS NOT NULL),
    CHECK (length(trim(holiday_name)) > 0),
    FOREIGN KEY (calendar_id) REFERENCES business_calendars(calendar_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(customer_id)) > 0),
    customer_segment TEXT NOT NULL CHECK (customer_segment IN ('consumer', 'small_business', 'enterprise')),
    preferred_language_id TEXT,
    country TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (length(trim(customer_segment)) > 0),
    CHECK (length(trim(country)) > 0),
    CHECK (julianday(created_at) IS NOT NULL),
    FOREIGN KEY (preferred_language_id) REFERENCES languages(language_id)
);

CREATE TABLE IF NOT EXISTS channels (
    channel_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(channel_id)) > 0),
    channel_name TEXT NOT NULL UNIQUE CHECK (channel_name IN ('email', 'chat', 'phone')),
    business_hours_start TEXT NOT NULL,
    business_hours_end TEXT NOT NULL,
    timezone TEXT NOT NULL,
    calendar_id TEXT NOT NULL,
    CHECK (time(business_hours_start) IS NOT NULL),
    CHECK (time(business_hours_end) IS NOT NULL),
    CHECK (business_hours_start <= business_hours_end),
    CHECK (length(trim(timezone)) > 0),
    FOREIGN KEY (calendar_id) REFERENCES business_calendars(calendar_id)
);

CREATE TABLE IF NOT EXISTS languages (
    language_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(language_id)) > 0),
    language_name TEXT NOT NULL UNIQUE CHECK (length(trim(language_name)) > 0)
);

CREATE TABLE IF NOT EXISTS intents (
    intent_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(intent_id)) > 0),
    intent_name TEXT NOT NULL UNIQUE CHECK (length(trim(intent_name)) > 0),
    default_priority TEXT NOT NULL CHECK (default_priority IN ('low', 'normal', 'high', 'critical'))
);

CREATE TABLE IF NOT EXISTS priority_rules (
    priority_rule_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(priority_rule_id)) > 0),
    priority TEXT NOT NULL CHECK (priority IN ('low', 'normal', 'high', 'critical')),
    channel_id TEXT NOT NULL,
    first_response_minutes INTEGER NOT NULL CHECK (first_response_minutes > 0),
    resolution_minutes INTEGER NOT NULL CHECK (resolution_minutes > 0),
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    CHECK (julianday(effective_from) IS NOT NULL),
    CHECK (effective_to IS NULL OR julianday(effective_to) IS NOT NULL),
    FOREIGN KEY (channel_id) REFERENCES channels(channel_id),
    CHECK (effective_to IS NULL OR julianday(effective_to) > julianday(effective_from)),
    UNIQUE (priority, channel_id, effective_from)
);

CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(agent_id)) > 0),
    agent_name TEXT NOT NULL,
    timezone TEXT NOT NULL,
    shift_start TEXT NOT NULL,
    shift_end TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    max_concurrency INTEGER NOT NULL CHECK (max_concurrency > 0),
    current_workload INTEGER NOT NULL DEFAULT 0 CHECK (current_workload >= 0),
    CHECK (length(trim(agent_name)) > 0),
    CHECK (length(trim(timezone)) > 0),
    CHECK (time(shift_start) IS NOT NULL),
    CHECK (time(shift_end) IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS skills (
    skill_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(skill_id)) > 0),
    skill_name TEXT NOT NULL UNIQUE CHECK (length(trim(skill_name)) > 0)
);

CREATE TABLE IF NOT EXISTS agent_skills (
    agent_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    PRIMARY KEY (agent_id, skill_id),
    CHECK (length(trim(agent_id)) > 0),
    CHECK (length(trim(skill_id)) > 0),
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE,
    FOREIGN KEY (skill_id) REFERENCES skills(skill_id)
);

CREATE TABLE IF NOT EXISTS agent_languages (
    agent_id TEXT NOT NULL,
    language_id TEXT NOT NULL,
    PRIMARY KEY (agent_id, language_id),
    CHECK (length(trim(agent_id)) > 0),
    CHECK (length(trim(language_id)) > 0),
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE,
    FOREIGN KEY (language_id) REFERENCES languages(language_id)
);

CREATE TABLE IF NOT EXISTS queues (
    queue_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(queue_id)) > 0),
    queue_name TEXT NOT NULL UNIQUE,
    intent_id TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK (capacity > 0),
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    CHECK (length(trim(queue_name)) > 0),
    FOREIGN KEY (intent_id) REFERENCES intents(intent_id)
);

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(ticket_id)) > 0),
    customer_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    language_id TEXT NOT NULL,
    priority TEXT NOT NULL CHECK (priority IN ('low', 'normal', 'high', 'critical')),
    queue_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('created', 'open', 'assigned', 'in_progress', 'pending', 'resolved', 'closed')),
    subject TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (length(trim(subject)) > 0),
    CHECK (julianday(created_at) IS NOT NULL),
    CHECK (julianday(updated_at) IS NOT NULL),
    CHECK (julianday(updated_at) >= julianday(created_at)),
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
    FOREIGN KEY (channel_id) REFERENCES channels(channel_id),
    FOREIGN KEY (intent_id) REFERENCES intents(intent_id),
    FOREIGN KEY (language_id) REFERENCES languages(language_id),
    FOREIGN KEY (queue_id) REFERENCES queues(queue_id)
);

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(message_id)) > 0),
    ticket_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    sender_type TEXT NOT NULL CHECK (sender_type IN ('customer', 'agent', 'system')),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (length(trim(body)) > 0),
    CHECK (julianday(created_at) IS NOT NULL),
    FOREIGN KEY (ticket_id) REFERENCES tickets(ticket_id) ON DELETE CASCADE,
    FOREIGN KEY (channel_id) REFERENCES channels(channel_id)
);

CREATE TABLE IF NOT EXISTS assignment_events (
    assignment_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(assignment_id)) > 0),
    ticket_id TEXT NOT NULL,
    agent_id TEXT,
    queue_id TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    unassigned_at TEXT,
    assignment_status TEXT NOT NULL CHECK (assignment_status IN ('assigned', 'transferred', 'unassigned', 'overflow')),
    decision_reason TEXT NOT NULL,
    assignment_number INTEGER NOT NULL CHECK (assignment_number > 0),
    FOREIGN KEY (ticket_id) REFERENCES tickets(ticket_id) ON DELETE CASCADE,
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id),
    FOREIGN KEY (queue_id) REFERENCES queues(queue_id),
    UNIQUE (ticket_id, assignment_number),
    CHECK (length(trim(decision_reason)) > 0),
    CHECK (unassigned_at IS NULL OR julianday(unassigned_at) >= julianday(assigned_at)),
    CHECK (julianday(assigned_at) IS NOT NULL),
    CHECK (unassigned_at IS NULL OR julianday(unassigned_at) IS NOT NULL),
    CHECK (assignment_status = 'overflow' OR agent_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS sla_events (
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

CREATE TABLE IF NOT EXISTS ticket_events (
    event_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(event_id)) > 0),
    ticket_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    actor_id TEXT,
    event_at TEXT NOT NULL,
    details TEXT NOT NULL,
    anomaly_type TEXT,
    CHECK (event_type IN (
        'created', 'opened', 'assigned', 'work_started', 'state_changed', 'pending', 'transferred',
        'escalated', 'reopened', 'resolved', 'closed', 'capacity_violation'
    )),
    CHECK (from_state IS NULL OR from_state IN (
        'created', 'open', 'assigned', 'in_progress', 'pending', 'resolved', 'closed'
    )),
    CHECK (to_state IS NULL OR to_state IN (
        'created', 'open', 'assigned', 'in_progress', 'pending', 'resolved', 'closed'
    )),
    CHECK (
        (event_type = 'created' AND from_state IS NULL AND to_state = 'created')
        OR (event_type = 'escalated' AND from_state IS NULL AND to_state IS NULL)
        OR (
            event_type NOT IN ('created', 'escalated')
            AND from_state IS NOT NULL
            AND to_state IS NOT NULL
        )
    ),
    CHECK (julianday(event_at) IS NOT NULL),
    CHECK (length(trim(details)) > 0),
    FOREIGN KEY (ticket_id) REFERENCES tickets(ticket_id) ON DELETE CASCADE,
    FOREIGN KEY (actor_id) REFERENCES agents(agent_id)
);

CREATE TABLE IF NOT EXISTS resolutions (
    resolution_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(resolution_id)) > 0),
    ticket_id TEXT NOT NULL,
    resolved_at TEXT,
    resolution_code TEXT NOT NULL,
    resolved INTEGER NOT NULL CHECK (resolved IN (0, 1)),
    resolution_minutes INTEGER CHECK (resolution_minutes IS NULL OR resolution_minutes >= 0),
    csat_score INTEGER CHECK (csat_score IS NULL OR csat_score BETWEEN 1 AND 5),
    CHECK (resolved_at IS NULL OR julianday(resolved_at) IS NOT NULL),
    CHECK ((resolved = 0 AND resolved_at IS NULL) OR (resolved = 1 AND resolved_at IS NOT NULL)),
    CHECK (length(trim(resolution_code)) > 0),
    FOREIGN KEY (ticket_id) REFERENCES tickets(ticket_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS anomalies (
    anomaly_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(anomaly_id)) > 0),
    ticket_id TEXT NOT NULL,
    anomaly_type TEXT NOT NULL CHECK (anomaly_type IN (
        'agent_over_capacity', 'sla_ignores_channel_calendar', 'reopen_loses_history',
        'transfer_loop', 'priority_safety_contradiction'
    )),
    description TEXT NOT NULL,
    expected_validation TEXT NOT NULL,
    ground_truth_label TEXT NOT NULL,
    CHECK (length(trim(anomaly_type)) > 0),
    CHECK (length(trim(description)) > 0),
    CHECK (length(trim(expected_validation)) > 0),
    CHECK (length(trim(ground_truth_label)) > 0),
    FOREIGN KEY (ticket_id) REFERENCES tickets(ticket_id)
);

CREATE TABLE IF NOT EXISTS ground_truth_labels (
    ticket_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(ticket_id)) > 0),
    expected_intent TEXT NOT NULL,
    expected_priority TEXT NOT NULL,
    expected_queue_id TEXT NOT NULL,
    expected_routing_outcome TEXT NOT NULL,
    expected_agent_id TEXT,
    expected_decision_reason TEXT NOT NULL,
    expected_sla_outcome TEXT NOT NULL,
    anomaly_type TEXT,
    expected_validation TEXT NOT NULL,
    CHECK (expected_priority IN ('low', 'normal', 'high', 'critical')),
    CHECK (expected_routing_outcome IN ('assigned', 'queue_overflow')),
    CHECK (expected_sla_outcome IN ('met', 'breached')),
    CHECK (anomaly_type IS NULL OR anomaly_type IN (
        'agent_over_capacity', 'sla_ignores_channel_calendar', 'reopen_loses_history',
        'transfer_loop', 'priority_safety_contradiction'
    )),
    FOREIGN KEY (ticket_id) REFERENCES tickets(ticket_id),
    FOREIGN KEY (expected_queue_id) REFERENCES queues(queue_id),
    FOREIGN KEY (expected_agent_id) REFERENCES agents(agent_id)
);

CREATE INDEX IF NOT EXISTS idx_tickets_queue_status ON tickets(queue_id, status);
CREATE INDEX IF NOT EXISTS idx_tickets_created_at ON tickets(created_at);
CREATE INDEX IF NOT EXISTS idx_messages_ticket_created ON messages(ticket_id, created_at);
CREATE INDEX IF NOT EXISTS idx_assignments_agent_active ON assignment_events(agent_id, unassigned_at);
CREATE INDEX IF NOT EXISTS idx_assignments_ticket_time ON assignment_events(ticket_id, assigned_at, assignment_number);
CREATE INDEX IF NOT EXISTS idx_sla_events_due ON sla_events(due_at, breached);
CREATE INDEX IF NOT EXISTS idx_sla_events_ticket_history ON sla_events(ticket_id, sla_type, started_at, sla_id);
CREATE INDEX IF NOT EXISTS idx_ticket_events_ticket_at ON ticket_events(ticket_id, event_at, event_id);
CREATE INDEX IF NOT EXISTS idx_ground_truth_anomaly_type ON ground_truth_labels(anomaly_type);

CREATE TRIGGER IF NOT EXISTS validate_ticket_timestamps_insert
BEFORE INSERT ON tickets
WHEN julianday(NEW.created_at) IS NULL
  OR julianday(NEW.updated_at) IS NULL
  OR julianday(NEW.updated_at) < julianday(NEW.created_at)
BEGIN
    SELECT RAISE(ABORT, 'ticket timestamps must be valid and updated_at must not precede created_at');
END;

CREATE TRIGGER IF NOT EXISTS validate_channel_calendar_insert
BEFORE INSERT ON channels
WHEN NOT EXISTS (
    SELECT 1
    FROM business_calendars
    WHERE calendar_id = NEW.calendar_id
      AND timezone = NEW.timezone
      AND business_hours_start = NEW.business_hours_start
      AND business_hours_end = NEW.business_hours_end
)
BEGIN
    SELECT RAISE(ABORT, 'channel timezone and hours must match its business calendar');
END;

CREATE TRIGGER IF NOT EXISTS validate_channel_calendar_update
BEFORE UPDATE OF calendar_id, timezone, business_hours_start, business_hours_end ON channels
WHEN NOT EXISTS (
    SELECT 1
    FROM business_calendars
    WHERE calendar_id = NEW.calendar_id
      AND timezone = NEW.timezone
      AND business_hours_start = NEW.business_hours_start
      AND business_hours_end = NEW.business_hours_end
)
BEGIN
    SELECT RAISE(ABORT, 'channel timezone and hours must match its business calendar');
END;

CREATE TRIGGER IF NOT EXISTS validate_calendar_channel_update
BEFORE UPDATE OF timezone, business_hours_start, business_hours_end ON business_calendars
WHEN EXISTS (
    SELECT 1
    FROM channels
    WHERE channels.calendar_id = OLD.calendar_id
      AND (
          channels.timezone <> NEW.timezone
          OR channels.business_hours_start <> NEW.business_hours_start
          OR channels.business_hours_end <> NEW.business_hours_end
      )
)
BEGIN
    SELECT RAISE(ABORT, 'calendar changes cannot invalidate channel timezone or hours');
END;

CREATE TRIGGER IF NOT EXISTS validate_ticket_timestamps_update
BEFORE UPDATE OF created_at, updated_at ON tickets
WHEN julianday(NEW.created_at) IS NULL
  OR julianday(NEW.updated_at) IS NULL
  OR julianday(NEW.updated_at) < julianday(NEW.created_at)
BEGIN
    SELECT RAISE(ABORT, 'ticket timestamps must be valid and updated_at must not precede created_at');
END;

CREATE TRIGGER IF NOT EXISTS validate_ticket_message_history_update
BEFORE UPDATE OF channel_id, created_at ON tickets
WHEN EXISTS (
    SELECT 1
    FROM messages
    WHERE messages.ticket_id = OLD.ticket_id
      AND (
          julianday(messages.created_at) < julianday(NEW.created_at)
          OR messages.channel_id <> NEW.channel_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'ticket changes cannot invalidate existing message history');
END;

CREATE TRIGGER IF NOT EXISTS validate_message_history_insert
BEFORE INSERT ON messages
WHEN julianday(NEW.created_at) IS NULL
  OR NOT EXISTS (
      SELECT 1
      FROM tickets
      WHERE tickets.ticket_id = NEW.ticket_id
        AND tickets.channel_id = NEW.channel_id
        AND julianday(NEW.created_at) >= julianday(tickets.created_at)
  )
BEGIN
    SELECT RAISE(ABORT, 'message timestamp and channel must match its ticket history');
END;

CREATE TRIGGER IF NOT EXISTS validate_message_history_update
BEFORE UPDATE OF ticket_id, channel_id, created_at ON messages
WHEN julianday(NEW.created_at) IS NULL
  OR NOT EXISTS (
      SELECT 1
      FROM tickets
      WHERE tickets.ticket_id = NEW.ticket_id
        AND tickets.channel_id = NEW.channel_id
        AND julianday(NEW.created_at) >= julianday(tickets.created_at)
  )
BEGIN
    SELECT RAISE(ABORT, 'message timestamp and channel must match its ticket history');
END;

CREATE TRIGGER IF NOT EXISTS validate_sla_channel_calendar_insert
BEFORE INSERT ON sla_events
WHEN NOT EXISTS (
    SELECT 1
    FROM tickets
    JOIN channels ON channels.channel_id = tickets.channel_id
    WHERE tickets.ticket_id = NEW.ticket_id
      AND channels.calendar_id = NEW.calendar_id
)
BEGIN
    SELECT RAISE(ABORT, 'SLA calendar must match the ticket channel calendar');
END;

CREATE TRIGGER IF NOT EXISTS validate_sla_channel_calendar_update
BEFORE UPDATE OF ticket_id, calendar_id ON sla_events
WHEN NOT EXISTS (
    SELECT 1
    FROM tickets
    JOIN channels ON channels.channel_id = tickets.channel_id
    WHERE tickets.ticket_id = NEW.ticket_id
      AND channels.calendar_id = NEW.calendar_id
)
BEGIN
    SELECT RAISE(ABORT, 'SLA calendar must match the ticket channel calendar');
END;

CREATE TRIGGER IF NOT EXISTS protect_sla_event_update
BEFORE UPDATE ON sla_events
BEGIN
    SELECT RAISE(ABORT, 'SLA events are append-only; insert a new history record');
END;

CREATE TRIGGER IF NOT EXISTS protect_sla_event_delete
BEFORE DELETE ON sla_events
BEGIN
    SELECT RAISE(ABORT, 'SLA events are append-only and cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS protect_ticket_event_update
BEFORE UPDATE ON ticket_events
BEGIN
    SELECT RAISE(ABORT, 'ticket lifecycle events are append-only and cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS protect_ticket_event_delete
BEFORE DELETE ON ticket_events
BEGIN
    SELECT RAISE(ABORT, 'ticket lifecycle events are append-only and cannot be deleted');
END;
