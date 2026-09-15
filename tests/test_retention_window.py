import pytest

from app.services.retention import validate_retention_window


def test_retention_at_least_as_long_as_topic_retention_is_valid():
    validate_retention_window(dedup_hours=192, topic_hours=168)


def test_retention_exactly_equal_to_topic_retention_is_valid():
    validate_retention_window(dedup_hours=168, topic_hours=168)


def test_retention_shorter_than_topic_retention_is_rejected():
    """
    The failure this guards against: a dedup record purged before Kafka
    would have stopped redelivering the event it guards — see
    app/services/retention.py's module docstring for the consequence.
    """
    with pytest.raises(ValueError, match="must be >="):
        validate_retention_window(dedup_hours=100, topic_hours=168)
