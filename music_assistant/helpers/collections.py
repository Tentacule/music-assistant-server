"""Helpers for collections."""

from collections.abc import Iterable, Mapping
from typing import Any

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import (
    MediaCollection,
    MediaItem,
    MediaItemCollection,
    UniqueList,
)

from music_assistant.constants import COLLECTION_ITEM_ID_SEPARATOR


def get_collection_item_id(collection_name: str, item_media_type: MediaType) -> str:
    """Get item id for a collection."""
    return COLLECTION_ITEM_ID_SEPARATOR.join((item_media_type.value.lower(), collection_name))


def get_collection_name_from_item_id(collection_item_id: str) -> str:
    """Get collection's name from item id."""
    return collection_item_id.split(COLLECTION_ITEM_ID_SEPARATOR, maxsplit=1)[1]


def get_collection_item_media_type_from_item_id(collection_item_id: str) -> MediaType:
    """Get media_type of items in a collection."""
    return MediaType(collection_item_id.split(COLLECTION_ITEM_ID_SEPARATOR, maxsplit=1)[0].lower())


def merge_item_collections(
    current: Iterable[MediaItemCollection] | None,
    update: Iterable[MediaItemCollection] | None,
) -> UniqueList[MediaItemCollection] | None:
    """
    Merge the collections an item is part of, keeping one entry per collection title.

    An item listed twice in the same collection would show up twice in it, so a collection
    given again replaces the one it has, unless it lacks the sequence the stored one knows.

    :param current: The collections the item is part of now.
    :param update: The collections given for the item.
    """
    if current is None and update is None:
        return None
    merged: dict[str, MediaItemCollection] = {}
    for collection in (*(current or ()), *(update or ())):
        existing = merged.get(collection.title)
        if existing and collection.sequence is None and existing.sequence is not None:
            continue
        merged[collection.title] = collection
    return UniqueList(merged.values())


def share_collections[K](
    collections_by_item: Mapping[K, Iterable[MediaItemCollection] | None],
) -> dict[K, UniqueList[MediaItemCollection]]:
    """
    Give each item the collections of all of them, and return the items that change.

    The versions of one album are in the same series, so a series known for one version
    holds for every version.

    :param collections_by_item: The collections of each item, by item key.
    """
    merged: UniqueList[MediaItemCollection] | None = None
    for collections in collections_by_item.values():
        merged = merge_item_collections(merged, collections)
    if not merged:
        return {}
    target = _collection_keys(merged)
    return {
        key: UniqueList(merged)
        for key, collections in collections_by_item.items()
        if _collection_keys(collections) != target
    }


def _collection_keys(
    collections: Iterable[MediaItemCollection] | None,
) -> set[tuple[str, Any]]:
    """Return the title and sequence of each collection, to compare two lists of them."""
    return {(collection.title, collection.sequence) for collection in collections or ()}


def has_available_item(items: Iterable[MediaItem | MediaCollection[Any]]) -> bool:
    """
    Return whether at least one item in a (possibly collapsed) listing is available.

    :param items: The listing to inspect; collections count as available when any of
        the items they hold is available.
    """
    for item in items:
        if isinstance(item, MediaCollection):
            # a collapsed collection carries no provider mappings of its own, so its
            # inherited `available` is always false: look at the items it holds instead
            if any(member.available for member in item.items):
                return True
        elif item.available:
            return True
    return False
