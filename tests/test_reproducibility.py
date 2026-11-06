from src.reproducibility import verify_reproducibility


def test_same_seed_and_configuration_reproduces_checksums():
    result = verify_reproducibility(
        seed=20261022, tickets=10, agents=3, messages=0, event_days=1
    )
    assert result["passed"] is True
    assert result["counts_match"] is True
    assert result["checksums_match"] is True
