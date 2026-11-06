# Assumptions and limitations

## Assumptions

- All customer, agent, message, and ticket data is synthetic; it must not be interpreted as production customer information.
- Generated timestamps use a UTC start-date anchor. Business calendars and agent schedules use `Asia/Kolkata` in the sample.
- Email is open Monday–Friday, chat Monday–Saturday, and phone is open every day for 24 hours. Two deterministic synthetic holidays are added to the email/chat calendars within the configured event window; the 24-hour phone calendar has no holidays.
- Base SLA targets are channel-adjusted priority rules; the exact values and business-minute algorithm are documented in `BUSINESS_RULES.md`.
- Complaint is the sample safety-sensitive intent and may not be below high priority.
- Ticket data is generated across `event_days`; lifecycle events and SLA completions can extend beyond a ticket's creation window because work may finish after a ticket is opened.
- At least five tickets and three agents are required so each anomaly has its own ticket and the controlled capacity/transfer fixtures remain deterministic.
- IDs derive from stable sequence numbers; they do not depend on database-generated keys.
- The `ground_truth` files and SQL table contain evaluation labels; exclude them from any model input feature set.

## Limitations

- Routing simulates sequential ticket arrival and releases; it is not a distributed queue worker and has no race control for concurrent production requests.
- Agent availability is represented by a weekly shift and active flag. Breaks, PTO, holidays for agents, skills proficiency, and shift handoff are not modeled.
- Queue capacity is a simple active-ticket count; channel-specific weighted capacity and aging policies are not modeled.
- Queue `active` is stored but not consulted by the simulator; generated queues are all active. There is no priority-ordered multi-ticket dispatch queue.
- Lifecycle helpers validate transitions, but the generator does not provide a general persisted reassignment service or a valid baseline reopen example. Transfer and reopen records are demonstrated by controlled anomaly fixtures; the reopen fixture deliberately omits its creation event.
- Calendar holidays are deterministic sample dates, not a jurisdiction-specific public-holiday feed. Daylight-saving changes are handled by Python `zoneinfo`, but the sample timezone does not use DST.
- `priority_rules` records effective ranges, but overlapping effective periods are not yet configuration-editable; generated rules have non-overlapping open-ended ranges per priority/channel.
- The lifecycle helper preserves prior event/SLA/assignment/resolution records on reopen; the generated reopen example is intentionally invalid and does not demonstrate a valid reopened baseline ticket.
- Dashboard is a local demonstration without authentication, authorization, or production hosting controls.
- Reproducibility is byte-for-byte within the same Python/Pandas/NumPy/SQLite environment and configuration. Different library/SQLite versions may change CSV/JSON/SQLite serialization; the manifest records Python/Pandas/NumPy versions and per-file hashes.
- Generated exports can be large at challenge scale. Regenerate locally and keep them out of source control.
