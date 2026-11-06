import json

from src.analytics import summarize
from src.generator import generate
from src.validate import validate


def test_analytics_covers_operations_sla_and_separate_anomaly_detections(tmp_path):
    output = tmp_path / "analytics-data"
    generate(
        seed=20261022,
        tickets=20,
        agents=5,
        messages=30,
        event_days=3,
        output_dir=output,
    )
    assert validate(output)

    summary = summarize(output)

    assert summary["ticket_count"] == 20
    assert sum(summary["tickets_by_status"].values()) == 20
    assert sum(item["tickets"] for item in summary["ticket_volume_by_day"]) == 20
    assert summary["sla"]["first_response"]["cycles"] == 20
    assert summary["sla"]["resolution"]["cycles"] == 20
    assert summary["sla_compliance_by_agent"]
    assert summary["sla_compliance_by_queue"]
    assert summary["sla_compliance_by_channel"]
    assert summary["sla_compliance_by_priority"]
    assert sum(row["assigned_ticket_count"] for row in summary["agent_workload"]) >= 20
    assert summary["agent_sla_duration_by_agent"]
    assert {
        "average_first_response_business_minutes",
        "average_resolution_business_minutes",
    } <= set(summary["agent_sla_duration_by_agent"][0])
    assert sum(item["transfers"] for item in summary["transfers_per_ticket"]) > 0
    loop_ticket = next(
        row for row in summary["transfers_per_ticket"]
        if row["ticket_id"] == "TKT-2026-000003"
    )
    assert loop_ticket["transfers"] == 2

    anomaly_summary = summary["anomaly_ground_truth_vs_detected"]
    assert anomaly_summary["detection_report_available"] is True
    assert anomaly_summary["validation_passed"] is True
    assert len(anomaly_summary["by_type"]) == 5
    assert all(row["injected"] == row["detected"] == 1
               for row in anomaly_summary["by_type"])
    assert anomaly_summary["precision_recall_f1"] is None
    saved = json.loads((output / "analytics_summary.json").read_text(encoding="utf-8"))
    assert saved["ticket_count"] == 20
