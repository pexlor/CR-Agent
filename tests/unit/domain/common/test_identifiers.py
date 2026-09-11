from uuid import UUID

import pytest

from code_review_agent.domain.common.identifiers import CheckpointId, TaskId


def test_task_id_is_lowercase_uuid_v4() -> None:
    task_id = TaskId.new()

    parsed = UUID(str(task_id))
    assert parsed.version == 4
    assert str(task_id) == str(task_id).lower()
    assert TaskId.parse(str(task_id)) == task_id


def test_identifier_rejects_noncanonical_or_non_v4_values() -> None:
    with pytest.raises(ValueError):
        TaskId.parse("{123e4567-e89b-12d3-a456-426614174000}")
    with pytest.raises(ValueError):
        TaskId.parse("123e4567-e89b-12d3-a456-426614174000")


def test_different_identifier_types_do_not_compare_equal() -> None:
    value = "123e4567-e89b-42d3-a456-426614174000"

    assert TaskId.parse(value) != CheckpointId.parse(value)
