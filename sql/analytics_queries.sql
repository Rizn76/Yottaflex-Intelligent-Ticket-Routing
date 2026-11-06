-- Total ticket volume and channel distribution.
SELECT c.channel_name, COUNT(*) AS ticket_count
FROM tickets t
JOIN channels c ON c.channel_id = t.channel_id
GROUP BY c.channel_name
ORDER BY ticket_count DESC;

-- Ticket volume by intent and priority.
SELECT i.intent_name, t.priority, COUNT(*) AS ticket_count
FROM tickets t
JOIN intents i ON i.intent_id = t.intent_id
GROUP BY i.intent_name, t.priority
ORDER BY i.intent_name, t.priority;

-- Agent assignments and peak-capacity candidates (assignment history is retained).
SELECT a.agent_id, a.agent_name, a.max_concurrency,
       COUNT(DISTINCT e.ticket_id) AS tickets_touched
FROM agents a
LEFT JOIN assignment_events e ON e.agent_id = a.agent_id
GROUP BY a.agent_id, a.agent_name, a.max_concurrency
ORDER BY tickets_touched DESC;

-- Backlog by queue, using the current ticket lifecycle state.
SELECT q.queue_name,
       SUM(CASE WHEN t.status IN ('open', 'assigned', 'in_progress', 'pending') THEN 1 ELSE 0 END) AS backlog,
       q.capacity
FROM queues q
LEFT JOIN tickets t ON t.queue_id = q.queue_id
GROUP BY q.queue_id, q.queue_name, q.capacity
ORDER BY backlog DESC;

-- SLA compliance by SLA type and priority.
SELECT s.sla_type, t.priority, COUNT(*) AS sla_count,
       SUM(s.breached) AS breached_count,
       1.0 - AVG(s.breached) AS compliance_rate
FROM sla_events s
JOIN tickets t ON t.ticket_id = s.ticket_id
GROUP BY s.sla_type, t.priority
ORDER BY s.sla_type, t.priority;

-- Escalation, transfer, and reopen rates by ticket event type.
SELECT event_type, COUNT(DISTINCT ticket_id) AS affected_tickets
FROM ticket_events
WHERE event_type IN ('escalated', 'transferred', 'reopened')
GROUP BY event_type
ORDER BY event_type;

-- Customer satisfaction for resolved tickets only.
SELECT AVG(csat_score) AS average_csat, COUNT(csat_score) AS scored_resolutions
FROM resolutions
WHERE resolved = 1;
