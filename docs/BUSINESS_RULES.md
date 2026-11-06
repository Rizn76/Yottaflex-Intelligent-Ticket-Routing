# Business rules and algorithms

## Intents and priority

Supported intents are billing, technical support, account access, sales, refund, delivery, and complaint. Priorities are low, normal, high, and critical, in increasing urgency.

- A complaint must be high or critical. The generator enforces that policy for valid baseline rows; the fifth intentional anomaly sets a complaint to low so validation can prove that the contradiction is detected.
- Priority rules are channel-specific and effective-dated. Base targets are critical 5/60, high 15/240, normal 60/1440, and low 240/4320 minutes for first response/resolution. Chat multiplies targets by 0.75; phone multiplies them by 0.5; targets are rounded up and never below one minute.
- Ground-truth priority is derived from the same documented policy. It is not independently randomized.
- Critical routing requires both the intent skill and an exact language match. It does not use a language fallback.

## Routing

At assignment time the engine evaluates every agent and returns a deterministic explanation, including each rejected agent's reasons, the selected agent's score, the eligible-agent count, and any fallback:

1. Agent is active.
2. Agent has the ticket's intent skill.
3. Agent is on shift in the agent's own time zone (including overnight shifts).
4. Current workload is below maximum concurrency.
5. Language matches, if possible; exact language matches are preferred.

Eligible agents are ranked by exact language first, then lowest workload-to-capacity ratio, lower workload, and stable agent ID. The reported score is 100 points for an exact language match plus 10 times the unused capacity fraction. A non-critical language mismatch is an explicit fallback and is recorded in the persisted explanation; critical tickets require an exact language match. There is no unskilled-agent fallback.

The persistent SQLite workflow runs queue selection, eligibility, assignment history, ticket state, and workload updates in one `BEGIN IMMEDIATE` transaction. Only tickets currently in `assigned` or `in_progress` consume agent capacity; moving to `open`, `pending`, `resolved`, or `closed` releases that capacity. `agents.current_workload` is maintained from live assignment history, rather than trusted as the capacity source. Queue backlog counts `open` tickets. An eligible agent may still take work when the queue is at capacity; when no agent is eligible and the backlog is full, an explicitly configured active overflow queue is used, or the ticket remains unassigned in its current queue. Unassigned attempts are retained as `overflow` assignment events with their fallback reason.

Queue dispatch order is critical, high, normal, then low priority, followed by oldest ticket creation time and ticket ID. Every assignment attempt has its own ID and per-ticket sequence number. Transfers close the prior assignment interval and append another event. A transfer path that returns to a previously visited agent after visiting another agent is a loop. Consecutive duplicate IDs alone are not considered a transfer loop.

## SLA calendars

Every SLA is either `first_response` or `resolution`. Its target is selected from the effective priority/channel rule and its channel calendar. Each calendar specifies an IANA timezone, local opening/closing times, at least one working weekday, and holiday dates; the weekday requirement is validated by the application because the relational schema permits a calendar to have no weekday rows.

`add_business_minutes(start, target, calendar)`:

1. Interprets a naive start in the calendar timezone, or converts an aware start to that timezone.
2. Moves to the next valid work window if the start is before opening, after closing, on a non-working weekday, or a holiday.
3. Consumes only the remaining minutes up to that day's closing time.
4. Repeats across later working days until the target is consumed.

When opening and closing times are equal, the schedule is a 24-hour calendar. For an actual event later than the SLA deadline, breach duration counts only working minutes after the deadline; elapsed-time breach is available explicitly for non-business-calendar use. A pending SLA is marked breached in the generated snapshot only when its deadline has passed by the configured event-window end.

Priority/channel targets are selected from the effective-dated `priority_rules` table at the start of each SLA cycle. The persistent workflow records first-response and resolution clocks when a routed ticket has no prior SLA history; reopening starts a new pair of clocks. Completion and first breach detection append new snapshots. SLA records are never updated or deleted. For pending clocks, the first check after the due time appends a breach snapshot; completion records the final actual time and business-minute breach duration.

Critical first-response escalation is scheduled five minutes before its SLA deadline, and critical resolution escalation fifteen minutes before its deadline, never earlier than the clock start. The escalation processor records one lifecycle event per SLA cycle and can move the ticket to an explicitly configured active escalation queue only when the move will not exceed that queue's open-ticket capacity. If full, the event is still recorded and the ticket remains in its existing queue and assignment state. A successful queue transfer closes the prior live assignment and leaves the ticket open and unassigned for dispatch. The generator stores start, due, actual, breach flag, business-minute breach duration, escalation time, and calendar key. The second intentional anomaly replaces one resolution deadline with elapsed-time arithmetic so deadline validation can identify it.

## Lifecycle

The persisted states are `created`, `open`, `assigned`, `in_progress`, `pending`, `resolved`, and `closed`. There is no separate `queued` or `waiting` state: `open` represents queued work and `pending` represents waiting for follow-up. Supported transitions are:

- `created -> open`
- `open -> assigned | pending | resolved`
- `assigned -> in_progress | pending | open | resolved`
- `in_progress -> pending | resolved | open`
- `pending -> assigned | in_progress | resolved | open`
- `resolved -> closed | open` (`open` is a reopen)
- `closed` is terminal.

The lifecycle service rejects invalid transitions and timestamps earlier than the ticket's update time or latest lifecycle event. It also rejects assigned/in-progress status without a live, non-overflow agent assignment. State changes append specific `opened`, `assigned`, `work_started`, `pending`, `resolved`, `closed`, or `reopened` events with time, optional actor, and reason. Transfer/escalation events are separate records. SQLite triggers prevent ticket-event updates/deletes. Resolution, pending, open, and closure release any live assignment; closing a resolved ticket does not create another SLA cycle. Reopening preserves existing events, assignments, and SLA rows, and appends a new first-response/resolution SLA cycle as documented above. Validation checks chronological/state-contiguous history and reconciles the last recorded state with `tickets.status`.

## Synthetic outcome and evaluation rules

The generator computes first-response and resolution times from source ticket attributes, priority rules, and calendars. Resolutions and CSAT use seeded distributions, but SLA targets/outcomes and expected routing/queue labels are rule-derived. The same seed and configuration drive every distribution.

By default, the generator injects exactly one deterministic scenario per required category after valid baseline generation. `py -m src.generator --no-anomalies` disables all injections and produces valid labels with `anomaly_type = NULL` and `expected_validation = VALID`.

1. **Agent over capacity:** concurrent assignments exceed the dedicated one-slot agent's limit → `CAPACITY_VIOLATION`.
2. **Calendar mismatch:** an SLA due time uses elapsed target minutes instead of the channel calendar → `SLA_DEADLINE_MISMATCH`.
3. **Reopen history loss:** one selected ticket lacks its original creation event while retaining its reopen event and other history → `MISSING_LIFECYCLE_HISTORY`.
4. **Transfer loop:** assignment destinations revisit the first agent after transfer to another agent → `TRANSFER_LOOP`.
5. **Safety priority contradiction:** a complaint's priority is injected as low despite the high-or-critical rule → `PRIORITY_CONTRADICTION`.

Each detailed row in `ground_truth/anomalies.csv`/`.json` includes deterministic anomaly ID, type, ticket, affected entity IDs, ticket-event/assignment/SLA references, reference timestamp, expected behavior, observed injection, explanation, severity, seed, and expected validation. `ground_truth/ground_truth.csv` contains one expected label per ticket. Neither file is included in operational feature exports or routing decisions. The normalized SQLite `anomalies` table stores its schema-defined subset; the full evaluation metadata stays in the separate ground-truth directory. When injections are enabled, validation requires exactly these five categories and no unrelated anomaly type; when disabled, it checks that anomaly labels are absent.

## Analytics definitions

- Ticket volume counts each ticket once by creation date (UTC date), channel, intent, priority, or current status. Queue backlog counts tickets currently `open`, `assigned`, `in_progress`, or `pending`.
- An unassigned ticket is `open` or `pending` with no live assignment to a non-null agent. Reopened-ticket counts use distinct tickets with a `reopened` event. Transfers count assignment-history rows marked `transferred`.
- Agent assigned/resolved totals are distinct tickets with an assignment record or a `resolved` event attributed to that agent. Per-agent first-response/resolution averages use completed business-calendar SLA cycles attributed to the first assignee. Peak utilization is peak concurrent assignment intervals divided by `max_concurrency`; workload imbalance is the maximum distinct assigned-ticket count divided by the mean across all agents.
- SLA analytics use the latest immutable row for each ticket, SLA type, and `started_at` cycle. Compliance is non-breached cycles divided by all cycles; average breach duration uses the persisted business-minute duration. Average response/resolution duration uses completed cycles only.
- SLA dimensions use current ticket priority, channel, and queue. Agent-level SLA is associated with the ticket's first recorded assignee and is not an assertion that the agent personally performed each action.
- “Approaching deadline” means a pending, not-yet-breached SLA due within the next 24 hours at analysis time. A ticket is considered to have reopened SLA cycles when it has more than one pair of first-response and resolution cycles.
- Anomaly analytics compare the separate injected ground truth with the validator's expected-anomaly detection records. Precision, recall, and F1 are not computed because the validation report does not classify the full negative population or false positives.
