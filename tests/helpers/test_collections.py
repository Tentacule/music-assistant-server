"""Tests for the collection helpers."""

from __future__ import annotations

import pytest
from music_assistant_models.helpers import set_global_cache_values
from music_assistant_models.media_items import (
    Audiobook,
    MediaCollection,
    MediaItemCollection,
    Podcast,
    ProviderMapping,
    UniqueList,
)

from music_assistant.helpers.collections import (
    has_available_item,
    merge_item_collections,
    share_collections,
)

pytestmark = pytest.mark.asyncio


def _mapping(provider_instance: str) -> ProviderMapping:
    return ProviderMapping(
        item_id=f"item_{provider_instance}",
        provider_domain=provider_instance,
        provider_instance=provider_instance,
    )


def _audiobook(name: str, provider_instance: str) -> Audiobook:
    return Audiobook(
        item_id=name,
        provider="library",
        name=name,
        provider_mappings={_mapping(provider_instance)},
    )


def _podcast(name: str, provider_instance: str) -> Podcast:
    return Podcast(
        item_id=name,
        provider="library",
        name=name,
        provider_mappings={_mapping(provider_instance)},
    )


async def test_has_available_item_plain_items() -> None:
    """Plain items count as available through their own provider mappings."""
    await set_global_cache_values({"available_providers": {"online"}})
    assert not has_available_item([])
    assert not has_available_item([_audiobook("Book", "offline")])
    assert has_available_item([_audiobook("Book", "offline"), _audiobook("Other", "online")])


async def test_has_available_item_looks_inside_collections() -> None:
    """A collection is available when any of its members is, regardless of its own mappings."""
    await set_global_cache_values({"available_providers": {"online"}})
    collection = MediaCollection[Podcast](
        item_id="podcast_collection",
        name="Series",
        provider="library",
        provider_mappings=set(),
        items=UniqueList([_podcast("Episode 1", "offline"), _podcast("Episode 2", "online")]),
    )
    # the collection itself has no provider mappings, so its inherited availability is false
    assert not collection.available
    assert has_available_item([collection])


async def test_has_available_item_unavailable_collection() -> None:
    """A collection whose members are all unavailable does not count as available."""
    await set_global_cache_values({"available_providers": {"online"}})
    collection = MediaCollection[Audiobook](
        item_id="book_collection",
        name="Series",
        provider="library",
        provider_mappings=set(),
        items=UniqueList([_audiobook("Book 1", "offline"), _audiobook("Book 2", "offline")]),
    )
    assert not has_available_item([collection])
    assert not has_available_item([collection, _audiobook("Book 3", "offline")])


async def test_merge_item_collections_keeps_one_entry_per_title() -> None:
    """A collection given again replaces the stored one, the others are kept."""
    merged = merge_item_collections(
        [MediaItemCollection(title="Now", sequence=3), MediaItemCollection(title="Hits")],
        [MediaItemCollection(title="Now", sequence=4)],
    )
    assert merged == [
        MediaItemCollection(title="Now", sequence=4),
        MediaItemCollection(title="Hits"),
    ]


async def test_merge_item_collections_keeps_known_sequence() -> None:
    """A collection given again without a sequence keeps the sequence it had."""
    merged = merge_item_collections(
        [MediaItemCollection(title="Now", sequence=3)], [MediaItemCollection(title="Now")]
    )
    assert merged == [MediaItemCollection(title="Now", sequence=3)]


async def test_merge_item_collections_none() -> None:
    """No collections on either side stays unknown."""
    assert merge_item_collections(None, None) is None
    assert merge_item_collections(None, []) == []


async def test_share_collections_gives_every_item_the_series_of_all() -> None:
    """A series known for one item reaches the others, and an item that has it stays."""
    saga = MediaItemCollection(title="Saga", sequence=2)
    changes = share_collections({1: [saga], 2: None, 3: [MediaItemCollection(title="Saga")]})

    assert set(changes) == {2, 3}
    assert [(entry.title, entry.sequence) for entry in changes[2]] == [("Saga", 2)]


async def test_share_collections_without_series_changes_nothing() -> None:
    """Items without any series stay as they are."""
    assert share_collections({1: None, 2: []}) == {}
