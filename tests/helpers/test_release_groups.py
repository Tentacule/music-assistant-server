"""Tests for the release group metadata helpers."""

from __future__ import annotations

from music_assistant_models.media_items import MediaItemCollection

from music_assistant.helpers.release_groups import shared_group_metadata


def test_versions_share_the_series_and_the_earliest_year() -> None:
    """Each version gets every series of its group and the earliest first release year."""
    saga = MediaItemCollection(title="Saga", sequence=1)

    changes = shared_group_metadata({1: ([saga], 1975), 2: ([], 1973), 3: ([saga], None)})

    assert changes == {
        1: {"original_year": 1973},
        2: {"collections": [saga.to_dict()]},
        3: {"original_year": 1973},
    }


def test_versions_that_agree_change_nothing() -> None:
    """A group whose versions already agree has nothing to share."""
    assert shared_group_metadata({1: ([], 1973), 2: ([], 1973)}) == {}
    assert shared_group_metadata({1: ([], None), 2: ([], None)}) == {}
