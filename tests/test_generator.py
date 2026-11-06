import json

import pandas as pd
import pytest

from src.generator import ANOMALY_TYPES, generate
from src.validate import validate


def test_small_generation_has_required_rows_ids_and_separate_labels(tmp_path):
    output = tmp_path / "dataset"
    manifest = generate(
        seed=17, tickets=20, agents=5, messages=35, event_days=3, output_dir=str(output)
    )
    tickets = pd.read_csv(output / "tickets.csv")
    messages = pd.read_csv(output / "messages.csv")
    labels = pd.read_csv(output / "ground_truth" / "ground_truth.csv")
    anomalies = pd.read_csv(output / "ground_truth" / "anomalies.csv")

    assert len(tickets) == manifest["counts"]["tickets"] == 20
    assert len(messages) == manifest["counts"]["messages"] == 35
    assert tickets["ticket_id"].iloc[0].startswith("TKT-2026-")
    assert messages["message_id"].str.match(r"MSG-TKT-2026-\d{6}-\d{3}").all()
    assert len(labels) == len(tickets)
    assert len(anomalies) == 5
    assert set(anomalies["anomaly_type"]) == set(ANOMALY_TYPES)
    assert anomalies["seed"].eq(17).all()
    assert {
        "affected_entity_ids",
        "event_references",
        "reference_timestamp",
        "expected_behavior",
        "observed_behavior",
        "severity",
    } <= set(anomalies.columns)
    for anomaly in anomalies.to_dict(orient="records"):
        assert json.loads(anomaly["affected_entity_ids"])
        references = json.loads(anomaly["event_references"])
        assert any(references.values())
        assert anomaly["expected_behavior"]
        assert anomaly["observed_behavior"]
    customers = pd.read_csv(output / "customers.csv").set_index("customer_id")
    assert tickets.apply(
        lambda ticket: customers.loc[ticket["customer_id"], "preferred_language_id"]
        == ticket["language_id"],
        axis=1,
    ).all()
    assert "current_workload" in pd.read_csv(output / "agents.csv").columns
    assert {"expected_agent_id", "expected_decision_reason"} <= set(labels.columns)
    assert "anomaly_type" not in tickets.columns
    operational_events = pd.read_csv(output / "ticket_events.csv")
    assert operational_events["anomaly_type"].isna().all()
    assert "anomaly_type" not in pd.read_csv(output / "assignment_events.csv").columns
    assert not (output / "ground_truth.csv").exists()
    assert not (output / "anomalies.csv").exists()
    assert pd.read_csv(output / "calendar_holidays.csv").query(
        "calendar_id == 'CAL-PHONE'"
    ).empty
    assert (output / "tickets.sqlite").exists()
    assert validate(output)


def test_anomaly_injection_can_be_disabled_without_label_leakage(tmp_path):
    output = tmp_path / "baseline-only"
    manifest = generate(
        seed=20261022,
        tickets=3,
        agents=2,
        messages=4,
        event_days=2,
        output_dir=output,
        inject_anomalies=False,
    )
    anomalies = pd.read_csv(output / "ground_truth" / "anomalies.csv")
    labels = pd.read_csv(output / "ground_truth" / "ground_truth.csv")
    events = pd.read_csv(output / "ticket_events.csv")

    assert manifest["configuration"]["inject_anomalies"] is False
    assert anomalies.empty
    assert labels["anomaly_type"].isna().all()
    assert labels["expected_validation"].eq("VALID").all()
    assert events["anomaly_type"].isna().all()
    assert validate(output)


@pytest.mark.parametrize(
    "options",
    [
        {"tickets": 4, "agents": 3, "messages": 0, "event_days": 1},
        {"tickets": 5, "agents": 2, "messages": 0, "event_days": 1},
        {"tickets": 5, "agents": 3, "messages": -1, "event_days": 1},
    ],
)
def test_invalid_scale_is_rejected(tmp_path, options):
    with pytest.raises(ValueError):
        generate(output_dir=str(tmp_path / "invalid"), **options)
