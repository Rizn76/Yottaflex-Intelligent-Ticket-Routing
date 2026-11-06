"""Streamlit dashboard for the generated YottaFlex support dataset."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from config import load_config
from src.analytics import agent_sla_duration_summary
from src.database import connect
from src.routing import process_sla_escalations, route_next_ticket

DATA_DIR = Path(load_config().output_dir)
DATABASE = DATA_DIR / "tickets.sqlite"
PAGES = [
    "Overview",
    "Ticket Analytics",
    "Agent Performance",
    "Queue Management",
    "SLA Monitoring",
    "Routing Decisions",
    "Anomaly Detection",
    "Data Validation",
]


@st.cache_data
def query(sql: str, parameters: tuple = ()) -> pd.DataFrame:
    if not DATABASE.exists():
        raise FileNotFoundError(f"Dataset database not found: {DATABASE}")
    with closing(sqlite3.connect(DATABASE)) as connection:
        return pd.read_sql_query(sql, connection, params=parameters)


def load_tickets() -> pd.DataFrame:
    return query("""
        SELECT t.ticket_id, t.created_at, t.updated_at, t.status, t.priority,
               c.channel_name AS channel, i.intent_name AS intent,
               l.language_name AS language, q.queue_id, q.queue_name AS queue
        FROM tickets t
        JOIN channels c ON c.channel_id = t.channel_id
        JOIN intents i ON i.intent_id = t.intent_id
        JOIN languages l ON l.language_id = t.language_id
        JOIN queues q ON q.queue_id = t.queue_id
    """)


@st.cache_data
def agent_utilization(ticket_ids: list[str]) -> pd.DataFrame:
    agents = query("""
        SELECT agent_id, agent_name, max_concurrency, current_workload FROM agents
    """)
    if not ticket_ids:
        agents["assigned_ticket_count"] = 0
        agents["resolved_ticket_count"] = 0
        agents["peak_concurrent_assignments"] = 0
        agents["peak_utilization"] = 0.0
        agents["average_first_response_business_minutes"] = None
        agents["average_resolution_business_minutes"] = None
        return agents
    assignments = query("""
        SELECT a.ticket_id, a.agent_id, a.assigned_at, a.unassigned_at, t.status
        FROM assignment_events a
        JOIN tickets t ON t.ticket_id = a.ticket_id
        WHERE a.agent_id IS NOT NULL
    """)
    assignments = assignments[assignments["ticket_id"].isin(ticket_ids)]
    resolved = query("""
        SELECT ticket_id, actor_id AS agent_id
        FROM ticket_events
        WHERE event_type = 'resolved' AND actor_id IS NOT NULL
    """)
    resolved = resolved[resolved["ticket_id"].isin(ticket_ids)].groupby(
        "agent_id", as_index=False
    )["ticket_id"].nunique().rename(columns={"ticket_id": "resolved_ticket_count"})
    events: dict[str, list[tuple[datetime, int]]] = defaultdict(list)
    for row in assignments.to_dict(orient="records"):
        agent_id = str(row["agent_id"])
        events[agent_id].append((datetime.fromisoformat(row["assigned_at"]), 1))
        if pd.notna(row["unassigned_at"]):
            events[agent_id].append((datetime.fromisoformat(row["unassigned_at"]), -1))
    assignment_counts = assignments.groupby("agent_id")["ticket_id"].nunique().to_dict()
    resolved_counts = dict(zip(resolved["agent_id"], resolved["resolved_ticket_count"]))
    current_counts = assignments.loc[
        assignments["unassigned_at"].isna()
        & assignments["status"].isin(["assigned", "in_progress"])
    ].groupby("agent_id")["ticket_id"].nunique().to_dict()
    records = []
    for agent in agents.to_dict(orient="records"):
        current = peak = 0
        for _, change in sorted(events[str(agent["agent_id"])], key=lambda item: (item[0], item[1])):
            current += change
            peak = max(peak, current)
        capacity = int(agent["max_concurrency"])
        records.append({
            **agent,
            "assigned_ticket_count": int(assignment_counts.get(agent["agent_id"], 0)),
            "resolved_ticket_count": int(resolved_counts.get(agent["agent_id"], 0)),
            "current_workload": int(current_counts.get(agent["agent_id"], 0)),
            "peak_concurrent_assignments": peak,
            "peak_utilization": peak / capacity,
        })
    metrics = pd.DataFrame(records)
    duration_metrics = pd.DataFrame(agent_sla_duration_summary(DATA_DIR, ticket_ids))
    if duration_metrics.empty:
        metrics["average_first_response_business_minutes"] = None
        metrics["average_resolution_business_minutes"] = None
        return metrics
    return metrics.merge(duration_metrics, on=["agent_id", "agent_name"], how="left")


@st.cache_data
def ticket_activity(ticket_ids: list[str]) -> tuple[int, pd.DataFrame, int]:
    reopened = query("""
        SELECT DISTINCT ticket_id
        FROM ticket_events
        WHERE event_type = 'reopened'
    """)
    reopened_count = int(reopened["ticket_id"].isin(ticket_ids).sum())
    transfers = query("""
        SELECT ticket_id, COUNT(*) AS transfers
        FROM assignment_events
        WHERE assignment_status = 'transferred'
        GROUP BY ticket_id
    """)
    transfers = transfers[transfers["ticket_id"].isin(ticket_ids)].sort_values(
        "transfers", ascending=False
    )
    unassigned = query("""
        SELECT t.ticket_id
        FROM tickets t
        WHERE t.status IN ('open', 'pending')
          AND NOT EXISTS (
              SELECT 1 FROM assignment_events a
              WHERE a.ticket_id = t.ticket_id
                AND a.agent_id IS NOT NULL
                AND a.unassigned_at IS NULL
          )
    """)
    unassigned_count = int(unassigned["ticket_id"].isin(ticket_ids).sum())
    return reopened_count, transfers, unassigned_count


st.set_page_config(page_title="YottaFlex Ticket Operations", layout="wide")
st.title("YottaFlex Support Operations")
st.caption("Synthetic ticket-routing and SLA dataset | Values are loaded from the generated SQLite database.")

if not DATABASE.exists():
    st.error(f"Database is missing: {DATABASE}. Generate the dataset first with `python -m src.generator`.")
    st.stop()

tickets = load_tickets()
if tickets.empty:
    st.info("The selected dataset contains no tickets.")
    st.stop()
channels = sorted(tickets["channel"].dropna().unique().tolist())
priorities = ["critical", "high", "normal", "low"]
queues = sorted(tickets["queue"].dropna().unique().tolist())
statuses = sorted(tickets["status"].dropna().unique().tolist())
selected_channels = st.sidebar.multiselect("Channels", channels, default=channels)
selected_priorities = st.sidebar.multiselect("Priorities", priorities, default=priorities)
selected_queues = st.sidebar.multiselect("Queues", queues, default=queues)
selected_statuses = st.sidebar.multiselect("Statuses", statuses, default=statuses)
ticket_dates = pd.to_datetime(tickets["created_at"], utc=True, errors="coerce").dt.date
date_range = st.sidebar.date_input(
    "Created date range",
    value=(ticket_dates.min(), ticket_dates.max()),
    min_value=ticket_dates.min(),
    max_value=ticket_dates.max(),
)
page = st.sidebar.selectbox("Dashboard page", PAGES)
filtered = tickets[
    tickets["channel"].isin(selected_channels)
    & tickets["priority"].isin(selected_priorities)
    & tickets["queue"].isin(selected_queues)
    & tickets["status"].isin(selected_statuses)
].copy()
if isinstance(date_range, tuple) and len(date_range) == 2:
    date_start, date_end = date_range
    filtered_dates = pd.to_datetime(filtered["created_at"], utc=True, errors="coerce").dt.date
    filtered = filtered.loc[filtered_dates.between(date_start, date_end)]
ticket_ids = filtered["ticket_id"].tolist()
if filtered.empty:
    st.info("No tickets match the selected filters. Adjust the date, channel, priority, queue, or status filters.")
    st.stop()

if page == "Overview":
    st.subheader("Operational overview")
    _, _, unassigned_count = ticket_activity(ticket_ids)
    sla = query("""
        WITH ranked_cycles AS (
            SELECT s.ticket_id, s.sla_type, s.breached,
                   ROW_NUMBER() OVER (
                       PARTITION BY s.ticket_id, s.sla_type, s.started_at
                       ORDER BY s.rowid DESC
                   ) AS cycle_rank
            FROM sla_events s
        )
        SELECT ticket_id, sla_type, breached
        FROM ranked_cycles
        WHERE cycle_rank = 1
    """)
    sla = sla[sla["ticket_id"].isin(ticket_ids)]
    resolution_sla = sla[sla["sla_type"] == "resolution"]
    compliance = 1 - float(resolution_sla["breached"].mean()) if not resolution_sla.empty else 0.0
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Tickets", f"{len(filtered):,}")
    col2.metric("Resolution SLA compliance", f"{compliance:.1%}")
    col3.metric("Open / pending", int(filtered["status"].isin(["open", "assigned", "in_progress", "pending"]).sum()))
    col4.metric("Critical", int((filtered["priority"] == "critical").sum()))
    col5.metric("Unassigned", unassigned_count)
    left, right = st.columns(2)
    left.plotly_chart(px.histogram(filtered, x="channel", color="priority", title="Tickets by channel and priority"), width="stretch")
    right.plotly_chart(px.histogram(filtered, x="intent", color="status", title="Tickets by intent and status"), width="stretch")
    daily = filtered.assign(created_day=pd.to_datetime(filtered["created_at"], utc=True).dt.date)
    st.plotly_chart(px.histogram(daily, x="created_day", title="Ticket volume over time"), width="stretch")
    st.dataframe(filtered.drop(columns=["updated_at", "queue_id"]).sort_values(
        "created_at", ascending=False
    ).head(30), width="stretch")

elif page == "Ticket Analytics":
    st.subheader("Ticket volume and lifecycle")
    reopened_count, transfer_counts, _ = ticket_activity(ticket_ids)
    total_transfers = int(transfer_counts["transfers"].sum()) if not transfer_counts.empty else 0
    activity_col1, activity_col2, activity_col3 = st.columns(3)
    activity_col1.metric("Reopened tickets", reopened_count)
    activity_col2.metric("Transfers", total_transfers)
    activity_col3.metric(
        "Average transfers per ticket",
        f"{total_transfers / len(filtered):.2f}" if len(filtered) else "0.00",
    )
    left, right = st.columns(2)
    left.plotly_chart(px.bar(filtered["channel"].value_counts().rename_axis("channel").reset_index(name="tickets"),
                             x="channel", y="tickets", title="Ticket volume by channel"), width="stretch")
    right.plotly_chart(px.bar(filtered["intent"].value_counts().rename_axis("intent").reset_index(name="tickets"),
                              x="intent", y="tickets", title="Ticket volume by intent"), width="stretch")
    daily = filtered.assign(created_day=pd.to_datetime(filtered["created_at"], utc=True).dt.date)
    st.plotly_chart(px.histogram(daily, x="created_day", title="Ticket volume over time"),
                    width="stretch")
    st.plotly_chart(px.histogram(filtered, x="priority", color="language", title="Priority mix by language"), width="stretch")
    status_counts = filtered["status"].value_counts().rename_axis("status").reset_index(name="tickets")
    st.plotly_chart(px.bar(status_counts, x="status", y="tickets", title="Ticket volume by status"),
                    width="stretch")
    if not transfer_counts.empty:
        st.subheader("Tickets with the most assignment transfers")
        st.dataframe(transfer_counts.head(20), width="stretch")
    st.dataframe(filtered[["ticket_id", "created_at", "channel", "intent", "language", "priority", "status", "queue"]],
                 width="stretch")

elif page == "Agent Performance":
    st.subheader("Agent workload and utilization")
    metrics = agent_utilization(ticket_ids)
    st.plotly_chart(px.bar(metrics.sort_values("peak_utilization", ascending=False),
                           x="agent_name", y="peak_utilization", color="peak_utilization",
                           hover_data=["peak_concurrent_assignments", "max_concurrency",
                                       "assigned_ticket_count", "resolved_ticket_count"],
                           title="Peak concurrency utilization by agent"), width="stretch")
    st.dataframe(metrics.sort_values("peak_utilization", ascending=False), width="stretch")

elif page == "Queue Management":
    st.subheader("Queue backlog")
    queue_metrics = query("SELECT queue_id, capacity FROM queues")
    status_counts = filtered.pivot_table(
        index="queue_id", columns="status", values="ticket_id", aggfunc="count", fill_value=0
    ).reset_index()
    queues = queue_metrics.merge(status_counts, on="queue_id", how="left").fillna(0)
    for status in ("open", "assigned", "in_progress", "pending"):
        if status not in queues:
            queues[status] = 0
    queues["open_backlog"] = queues["open"]
    queues["capacity_utilization"] = queues["open_backlog"] / queues["capacity"]
    queue_names = filtered[["queue_id", "queue"]].drop_duplicates().set_index("queue_id")["queue"]
    queues["queue_name"] = queues["queue_id"].map(queue_names)
    queues = queues[queues["queue_id"].isin(filtered["queue_id"])].copy()
    st.plotly_chart(px.bar(queues, x="queue_name", y="open_backlog", color="open_backlog",
                           hover_data=["capacity"], title="Backlog by queue"), width="stretch")
    st.dataframe(queues, width="stretch")
    st.dataframe(filtered[filtered["status"].isin(["open", "assigned", "in_progress", "pending"])],
                 width="stretch")

elif page == "SLA Monitoring":
    st.subheader("SLA performance")
    sla = query("""
        SELECT s.ticket_id, s.sla_type, s.target_minutes, s.started_at, s.due_at,
               s.actual_at, s.breached, s.breach_minutes, t.priority,
               c.channel_name AS channel, q.queue_name AS queue, g.agent_name,
               first_assignment.agent_id
        FROM sla_events s JOIN tickets t ON t.ticket_id = s.ticket_id
        JOIN channels c ON c.channel_id = t.channel_id
        JOIN queues q ON q.queue_id = t.queue_id
        LEFT JOIN assignment_events first_assignment
          ON first_assignment.ticket_id = t.ticket_id
         AND first_assignment.assignment_number = 1
        LEFT JOIN agents g ON g.agent_id = first_assignment.agent_id
        WHERE s.rowid = (
            SELECT latest.rowid
            FROM sla_events latest
            WHERE latest.ticket_id = s.ticket_id
              AND latest.sla_type = s.sla_type
              AND latest.started_at = s.started_at
            ORDER BY latest.rowid DESC
            LIMIT 1
        )
    """)
    sla = sla[sla["ticket_id"].isin(ticket_ids)]
    summary = sla.groupby("sla_type").agg(
        current_cycles=("ticket_id", "count"),
        breaches=("breached", "sum"),
        average_breach_minutes=("breach_minutes", "mean"),
    ).reset_index()
    summary["compliance_rate"] = 1 - summary["breaches"] / summary["current_cycles"]
    st.dataframe(summary, width="stretch")
    st.caption("SLA rows show the latest immutable snapshot for each ticket, SLA type, and start time.")
    cycle_counts = sla.groupby("ticket_id")["sla_type"].count()
    st.metric("Tickets with reopened SLA cycles", int((cycle_counts > 2).sum()))
    for dimension, title in (("channel", "Channel"), ("priority", "Priority"),
                             ("queue", "Queue"), ("agent_name", "Assigned agent")):
        grouped = sla.groupby([dimension, "sla_type"], dropna=False).agg(
            cycles=("ticket_id", "count"), breaches=("breached", "sum")
        ).reset_index()
        grouped["compliance_rate"] = 1 - grouped["breaches"] / grouped["cycles"]
        st.subheader(f"SLA compliance by {title.lower()}")
        st.dataframe(grouped, width="stretch")
    st.plotly_chart(px.histogram(sla, x="breach_minutes", color="sla_type",
                                 title="Business-calendar breach minutes"), width="stretch")
    now = pd.Timestamp.now(tz="UTC")
    due_at = pd.to_datetime(sla["due_at"], utc=True, errors="coerce")
    approaching = sla[
        sla["actual_at"].isna()
        & sla["breached"].astype(int).eq(0)
        & due_at.ge(now)
        & due_at.le(now + pd.Timedelta(hours=24))
    ]
    st.metric("Open SLA clocks due in the next 24 hours", len(approaching))
    if not approaching.empty:
        st.dataframe(approaching[["ticket_id", "sla_type", "due_at", "priority", "channel"]],
                     width="stretch")
    st.dataframe(sla.sort_values(["breached", "breach_minutes"], ascending=False).head(100),
                 width="stretch")
    with st.expander("Append-only SLA event history"):
        history = query("""
            SELECT sla_id, ticket_id, sla_type, started_at, due_at, actual_at,
                   breached, breach_minutes, escalation_at
            FROM sla_events
        """)
        st.dataframe(history[history["ticket_id"].isin(ticket_ids)], width="stretch")

elif page == "Routing Decisions":
    st.subheader("Explainable assignment history")
    st.warning("Routing and escalation actions write to this SQLite database. Use a copy if you need to preserve generated-data checksums.")
    queue_options = query("SELECT queue_id, queue_name FROM queues WHERE active = 1 ORDER BY queue_name")
    if not queue_options.empty:
        queue_ids = queue_options["queue_id"].tolist()
        queue_names = dict(zip(queue_options["queue_id"], queue_options["queue_name"]))
        selected_queue = st.selectbox(
            "Dispatch queue",
            queue_ids,
            format_func=lambda queue_id: f"{queue_names[queue_id]} ({queue_id})",
        )
        overflow_choice = st.selectbox(
            "Overflow queue",
            [""] + queue_ids,
            format_func=lambda queue_id: "Keep on current queue" if not queue_id else (
                f"{queue_names[queue_id]} ({queue_id})"
            ),
        )
        if st.button("Route next open ticket"):
            try:
                with closing(connect(DATABASE)) as connection:
                    decision = route_next_ticket(
                        connection,
                        selected_queue,
                        now=datetime.now().astimezone(),
                        overflow_queue_id=overflow_choice or None,
                    )
                st.cache_data.clear()
                if decision is None:
                    st.info("No open tickets are waiting in this queue.")
                else:
                    st.success(f"Routing decision saved for {decision['ticket_id']}.")
                    st.json(decision)
                st.rerun()
            except (sqlite3.Error, LookupError, ValueError) as error:
                st.error(f"Routing action failed: {error}")

        escalation_queue = st.selectbox(
            "Escalation queue (optional)",
            [""] + queue_ids,
            format_func=lambda queue_id: "Record escalation without moving queues" if not queue_id else (
                f"{queue_names[queue_id]} ({queue_id})"
            ),
        )
        if st.button("Process due SLA escalations"):
            try:
                with closing(connect(DATABASE)) as connection:
                    escalations = process_sla_escalations(
                        connection,
                        datetime.now().astimezone(),
                        escalation_queue_id=escalation_queue or None,
                    )
                st.cache_data.clear()
                if escalations:
                    st.success(f"Recorded {len(escalations)} due escalation(s).")
                    st.dataframe(pd.DataFrame(escalations), width="stretch")
                else:
                    st.info("No unprocessed SLA escalations are due.")
                st.rerun()
            except (sqlite3.Error, LookupError, ValueError) as error:
                st.error(f"Escalation action failed: {error}")

    decisions = query("""
        SELECT a.ticket_id, a.assignment_number, a.assigned_at, a.unassigned_at,
               a.assignment_status, a.decision_reason, a.agent_id, g.agent_name,
               q.queue_name, t.priority, i.intent_name AS intent, l.language_name AS language
        FROM assignment_events a
        LEFT JOIN agents g ON g.agent_id = a.agent_id
        JOIN queues q ON q.queue_id = a.queue_id
        JOIN tickets t ON t.ticket_id = a.ticket_id
        JOIN intents i ON i.intent_id = t.intent_id
        JOIN languages l ON l.language_id = t.language_id
    """)
    decisions = decisions[decisions["ticket_id"].isin(ticket_ids)]
    st.dataframe(decisions.sort_values(["ticket_id", "assignment_number"]), width="stretch")
    ticket_choice = st.selectbox("Ticket details", filtered["ticket_id"].tolist())
    st.json(decisions[decisions["ticket_id"] == ticket_choice].to_dict(orient="records"))

elif page == "Anomaly Detection":
    st.subheader("Injected ground truth vs validator detections")
    st.caption("Ground-truth labels are evaluation-only and are not used by routing or SLA workflows.")
    anomaly_path = DATA_DIR / "ground_truth" / "anomalies.csv"
    if not anomaly_path.exists():
        st.warning("Anomaly labels are missing. Regenerate the dataset.")
    else:
        anomalies = pd.read_csv(anomaly_path)
        anomalies = anomalies[anomalies["ticket_id"].isin(ticket_ids)]
        report_path = DATA_DIR / "validation_report.json"
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
        detected = pd.DataFrame(report.get("expected_anomalies", []))
        if not detected.empty:
            detected = detected[detected["ticket_id"].isin(ticket_ids)]
            detections_by_type = detected.groupby("anomaly_type")["detected"].sum().to_dict()
        else:
            detections_by_type = {}
        injected_by_type = anomalies["anomaly_type"].value_counts().to_dict()
        comparison_types = sorted(set(injected_by_type) | set(detections_by_type))
        comparison = pd.DataFrame([
            {
                "anomaly_type": anomaly_type,
                "injected_ground_truth": int(injected_by_type.get(anomaly_type, 0)),
                "detected_by_validator": int(detections_by_type.get(anomaly_type, 0)),
            }
            for anomaly_type in comparison_types
        ])
        st.metric("Injected anomalies in filter", len(anomalies))
        st.metric("Detected expected anomalies in filter", int(comparison["detected_by_validator"].sum())
                  if not comparison.empty else 0)
        st.dataframe(comparison, width="stretch")
        if not comparison.empty:
            distribution = comparison.melt(
                id_vars="anomaly_type",
                var_name="source",
                value_name="count",
            )
            st.plotly_chart(
                px.bar(
                    distribution,
                    x="anomaly_type",
                    y="count",
                    color="source",
                    barmode="group",
                    title="Injected vs detected anomalies",
                ),
                width="stretch",
            )
        st.dataframe(anomalies.drop(
            columns=["affected_entity_ids", "event_references"], errors="ignore"
        ), width="stretch")

else:
    st.subheader("Data validation results")
    report_path = DATA_DIR / "validation_report.json"
    summary_path = DATA_DIR / "validation_summary.txt"
    if not report_path.exists():
        st.warning("Run `python -m src.validate` to create the validation report.")
    else:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        st.metric("Validation", "PASSED" if report.get("passed") else "FAILED")
        expected = report.get("expected_anomalies", [])
        st.write(f"Expected anomaly detections: {sum(item.get('detected', False) for item in expected)}/{len(expected)}")
        if summary_path.exists():
            st.code(summary_path.read_text(encoding="utf-8"))
        st.dataframe(pd.DataFrame(expected), width="stretch")
        if report.get("unexpected_errors"):
            st.error("\n".join(report["unexpected_errors"]))
