"""Serve the library albums deduplicated by MusicBrainz release group."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

import aiohttp
from music_assistant_models.auth import Scope
from music_assistant_models.enums import (
    AlbumType,
    EventType,
    ExternalID,
    ListingType,
    MediaType,
    SortDirection,
    SortField,
)
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.helpers import create_safe_string
from music_assistant_models.media_items import (
    Album,
    MediaCollection,
    MediaItemCollection,
    UniqueList,
)

from music_assistant.constants import DB_TABLE_ALBUMS, DB_TABLE_EXTERNAL_ID_LOOKUP
from music_assistant.controllers.music.custom_migrations import (
    DB_TABLE_ALBUM_GROUP_DEFAULTS,
    DB_TABLE_ALBUM_SERIES_HIDDEN,
)
from music_assistant.controllers.music.helpers import (
    search_name_match_clause,
    series_filter_clause,
)
from music_assistant.controllers.music.listing import apply_listing, resolve_listing_sort
from music_assistant.helpers.collections import (
    get_collection_item_id,
    get_collection_name_from_item_id,
)
from music_assistant.helpers.external_ids import barcode_to_upc, is_valid_barcode
from music_assistant.helpers.json import json_loads
from music_assistant.models.music_provider import PROVIDER_FETCH_ERRORS

from .base import MediaControllerBase

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from music_assistant.providers.musicbrainz import MusicbrainzProvider

    _Base = MediaControllerBase[Album]
else:
    _Base = object

_API_BASE: Final[str] = "music/album_groups"
_SEARCH_ARTIST_FALLBACK_THRESHOLD: Final[int] = 25
_MAX_GROUP_VERSIONS: Final[int] = 500
# each lookup of an unknown barcode costs a throttled MusicBrainz request
_MAX_BARCODE_LOOKUPS: Final[int] = 15
_BARCODE_CACHE_EXPIRATION: Final[int] = 86400 * 30
_BARCODE_CACHE_PROVIDER: Final[str] = "album_groups"
_LOOKUP_ERRORS: Final[tuple[type[Exception], ...]] = (
    *PROVIDER_FETCH_ERRORS,
    aiohttp.ClientResponseError,
    InvalidDataError,
)
# an album can carry several ids after a merge: the lowest is its one stable group
_RELEASE_GROUP_SQL: Final[str] = (
    f"(SELECT MIN({DB_TABLE_EXTERNAL_ID_LOOKUP}.external_id) "
    f"FROM {DB_TABLE_EXTERNAL_ID_LOOKUP} "
    f"WHERE {DB_TABLE_EXTERNAL_ID_LOOKUP}.media_type = 'album' "
    f"AND {DB_TABLE_EXTERNAL_ID_LOOKUP}.external_id_type = "
    f"'{ExternalID.MB_RELEASEGROUP.value}' "
    f"AND {DB_TABLE_EXTERNAL_ID_LOOKUP}.item_id = {DB_TABLE_ALBUMS}.item_id)"
)
_GROUP_KEY_SQL: Final[str] = (
    f"COALESCE({_RELEASE_GROUP_SQL}, 'album:' || {DB_TABLE_ALBUMS}.item_id)"
)


@dataclass
class AlbumGroupInfo:
    """Where a library album stands within its release group."""

    release_group_id: str | None = None
    version_count: int = 1
    default_item_id: str | None = None
    is_default: bool = False
    can_set_default: bool = False


class AlbumGroupsMixin(_Base):
    """Release-group listings of the albums controller, next to the per-album commands."""

    if TYPE_CHECKING:
        versions: Callable[..., Awaitable[list[Album]]]

    def _register_album_group_commands(self) -> None:
        """Register the release-group API commands."""
        for name, handler, scope in (
            ("library_items", self.album_group_items, Scope.LIBRARY_READ),
            ("count", self.album_group_count, Scope.LIBRARY_READ),
            ("artist_albums", self.album_group_artist_albums, Scope.LIBRARY_READ),
            ("versions", self.album_group_versions, Scope.LIBRARY_READ),
            ("info", self.album_group_info, Scope.LIBRARY_READ),
            ("set_default", self.set_album_group_default, Scope.LIBRARY_WRITE),
            ("series", self.album_series, Scope.LIBRARY_READ),
            ("hidden_series", self.hidden_album_series, Scope.LIBRARY_READ),
            ("set_series_hidden", self.set_album_series_hidden, Scope.LIBRARY_WRITE),
        ):
            self.mass.register_api_command(f"{_API_BASE}/{name}", handler, required_scope=scope)

    async def album_group_items(  # noqa: PLR0913
        self,
        favorite: bool | None = None,
        search: str | None = None,
        limit: int = 500,
        offset: int = 0,
        order_by: str | None = None,
        provider: str | list[str] | None = None,
        genre: int | list[int] | None = None,
        played_only: bool = False,
        album_types: list[AlbumType] | None = None,
        *,
        sort_field: SortField | None = None,
        sort_direction: SortDirection | None = None,
        summary: bool = True,
        collapse_collections: bool = False,
        series_filter: str = "all",
        reachable_via: list[str] | None = None,
        random_seed: int | None = None,
    ) -> list[Album | MediaCollection[Album]]:
        """
        Return the library albums with one item per release group.

        The item of a group is its default version when that one matches the filters, else
        the matching version added last. An album without a release group stands on its own.

        :param favorite: Only include the current user's likes (True) or dislikes (False).
        :param search: Filter by search query.
        :param limit: Maximum number of items to return.
        :param offset: Number of items to skip.
        :param order_by: DEPRECATED - use sort_field and sort_direction instead.
        :param provider: Filter by provider instance ID (single string or list).
        :param genre: Filter by genre id(s).
        :param played_only: Filter to only played albums.
        :param album_types: Filter by album types.
        :param sort_field: Sort field to use.
        :param sort_direction: Sort direction, the field's default when omitted.
        :param summary: Return slim summary items instead of fully hydrated items.
        :param collapse_collections: Return the albums of a series as one MediaCollection.
        :param series_filter: "all" (default), "albums" for the albums in no series only,
            "series" for the albums in a series only.
        :param reachable_via: Restrict to items reachable through these provider instance ids.
        :param random_seed: Keep random ordering stable across pages with the same seed.
        """
        field, direction = self.resolve_sort(sort_field, sort_direction, order_by)
        reachable_via = self._resolve_reachable_via(reachable_via)
        if reachable_via is not None and not reachable_via:
            return []
        provider_filter = self._provider_filter_considering_reachability(provider, reachable_via)
        params: dict[str, Any] = {}
        parts: list[str] = []
        joins: list[str] = []
        artist_join = (
            "JOIN album_artists ON album_artists.album_id = albums.item_id "
            "JOIN artists ON artists.item_id = album_artists.artist_id"
        )
        if album_types:
            parts.append("albums.album_type IN :album_types")
            params["album_types"] = [x.value for x in album_types]
        if series_clause := series_filter_clause(series_filter):
            parts.append(series_clause)
        artists_joined = field == SortField.ARTIST_NAME
        if artists_joined:
            joins.append(artist_join)
        if search and " - " in search:
            artist_str, title_str = search.split(" - ", 1)
            search = None
            parts.append(
                search_name_match_clause(
                    "albums", create_safe_string(title_str, True, True), "search_title", params
                )
            )
            artist_clause = search_name_match_clause(
                "artists", create_safe_string(artist_str, True, True), "search_artist", params
            )
            if artists_joined:
                parts.append(artist_clause)
            else:
                joins.append(f"{artist_join} AND {artist_clause}")
                artists_joined = True

        async def _fetch(
            query_search: str | None,
            query_parts: list[str],
            query_joins: list[str],
            query_limit: int,
            query_offset: int,
        ) -> list[Album | MediaCollection[Album]]:
            query_params = dict(params)
            clause = self._representative_clause(
                query_parts,
                query_joins,
                query_params,
                favorite=favorite,
                # a collapsed listing searches the names of the series and albums it lists
                search=None if collapse_collections else query_search,
                genre=genre,
                provider_filter=provider_filter,
                played_only=played_only,
                reachable_via=reachable_via,
            )
            return await self.get_library_items_by_query(
                favorite=favorite,
                search=query_search,
                genre_ids=genre,
                limit=query_limit,
                offset=query_offset,
                sort_field=field,
                sort_direction=direction,
                provider_filter=provider_filter,
                extra_query_parts=[*query_parts, clause],
                extra_query_params=query_params,
                extra_join_parts=query_joins,
                played_only=played_only,
                in_library_only=True,
                summary=summary,
                collapse_collections=collapse_collections,
                reachable_via=reachable_via,
                random_seed=random_seed,
            )

        result = await _fetch(search, parts, joins, limit, offset)
        remaining = limit - len(result)
        if search and len(result) < _SEARCH_ARTIST_FALLBACK_THRESHOLD and not offset and remaining:
            # a search also lists the albums of the artists it matches, after the title matches
            artist_clause = search_name_match_clause(
                "artists", create_safe_string(search, True, True), "search_artist", params
            )
            if artists_joined:
                extra_parts, extra_joins = [*parts, artist_clause], joins
            else:
                extra_parts, extra_joins = parts, [*joins, f"{artist_join} AND {artist_clause}"]
            by_artist = await _fetch(None, extra_parts, extra_joins, remaining, 0)
            known_uris = {item.uri for item in result}
            result = await self._collapse_groups(
                [*result, *(item for item in by_artist if item.uri not in known_uris)]
            )
            result = result[:limit]
        return result

    async def album_group_count(
        self, favorite_only: bool = False, album_types: list[AlbumType] | None = None
    ) -> int:
        """
        Return the number of library albums, counting a release group once.

        :param favorite_only: Only count the albums the current user likes.
        :param album_types: Only count albums of these types.
        """
        parts: list[str] = []
        params: dict[str, Any] = {}
        if favorite_only:
            parts.append(self._favorite_filter_clause(params, True))
        if album_types:
            parts.append("albums.album_type IN :album_types")
            params["album_types"] = [x.value for x in album_types]
        parts.append(
            self._provider_filter_clause(
                params, self._ensure_provider_filter(None), in_library_only=True
            )
        )
        query = (
            f"SELECT DISTINCT {_GROUP_KEY_SQL} AS group_key FROM {DB_TABLE_ALBUMS} "
            f"WHERE {' AND '.join(parts)}"
        )
        return await self.mass.music.database.get_count_from_query(query, params)

    async def album_group_artist_albums(  # noqa: PLR0913
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        provider_filter: str | None = None,
        search: str | None = None,
        sort_field: SortField | None = None,
        sort_direction: SortDirection | None = None,
        limit: int | None = 500,
        offset: int = 0,
        album_types: list[AlbumType] | None = None,
        favorite: bool | None = None,
        random_seed: int | None = None,
        collapse_collections: bool = False,
        series_filter: str = "all",
    ) -> list[Album | MediaCollection[Album]]:
        """
        Return the albums of an artist with one library item per release group.

        :param item_id: The item ID of the artist.
        :param provider_instance_id_or_domain: The provider instance ID or domain of the artist.
        :param provider_filter: Optional provider instance ID to limit the (library) result to.
        :param search: Only list the albums whose name or artist name contains this text.
        :param sort_field: Sort field, the sort name when omitted.
        :param sort_direction: Sort direction, the field's default when omitted.
        :param limit: Maximum number of albums to return; 0 returns them all.
        :param offset: Number of albums to skip.
        :param album_types: Only list albums whose type is included.
        :param favorite: Only list the albums the current user likes (True) or dislikes (False).
        :param random_seed: Keep random ordering stable across pages with the same seed.
        :param collapse_collections: Return the albums of a series as one MediaCollection,
            at the place of its first album.
        :param series_filter: "all" (default), "albums" for the albums in no series only,
            "series" for the albums in a series only.
        """
        albums = await self.mass.music.artists.albums(
            item_id,
            provider_instance_id_or_domain,
            provider_filter=provider_filter,
            search=search,
            sort_field=sort_field,
            sort_direction=sort_direction,
            limit=0,
            album_types=album_types,
            favorite=favorite,
            random_seed=random_seed,
        )
        collapsed: list[Album | MediaCollection[Album]] = list(await self._collapse_groups(albums))
        if collapse_collections or series_filter in ("albums", "series"):
            collapsed = await self._apply_series(collapsed, collapse_collections, series_filter)
        return collapsed[offset : offset + limit] if limit else collapsed[offset:]

    async def _apply_series(
        self,
        albums: list[Album | MediaCollection[Album]],
        collapse_collections: bool,
        series_filter: str,
    ) -> list[Album | MediaCollection[Album]]:
        """
        Filter a listing of albums on their series and optionally group each series.

        The series of a library album are read from the library, so a provider album is in
        no series. An album in two series shows up in both.

        :param albums: The albums, in the order they are listed.
        :param collapse_collections: Replace the albums of a series by one MediaCollection.
        :param series_filter: "albums" keeps the albums in no series, "series" those in one.
        """
        series_of = await self._library_album_series(albums)
        result: list[Album | MediaCollection[Album]] = []
        groups: dict[str, MediaCollection[Album]] = {}
        sequences: dict[str, list[tuple[Any, Album]]] = {}
        for album in albums:
            series = series_of.get(album.item_id, []) if _is_library_album(album) else []
            if series_filter == "albums" and series:
                continue
            if series_filter == "series" and not series:
                continue
            if not collapse_collections or not series:
                result.append(album)
                continue
            for collection in series:
                if collection.title not in groups:
                    groups[collection.title] = MediaCollection(
                        item_id=get_collection_item_id(collection.title, MediaType.ALBUM),
                        provider="library",
                        name=collection.title,
                        provider_mappings=set(),
                        items=UniqueList(),
                    )
                    sequences[collection.title] = []
                    result.append(groups[collection.title])
                sequences[collection.title].append((collection.sequence, cast("Album", album)))
        for title, group in groups.items():
            group.items = UniqueList(
                album for _, album in sorted(sequences[title], key=lambda x: _sequence_key(x[0]))
            )
        return result

    async def _library_album_series(
        self, albums: list[Album | MediaCollection[Album]]
    ) -> dict[str, list[MediaItemCollection]]:
        """Return the series of each library album in the listing, by item id."""
        library_ids = [int(album.item_id) for album in albums if _is_library_album(album)]
        if not library_ids:
            return {}
        rows = await self.mass.music.database.get_rows_from_query(
            f"""SELECT item_id, json_extract(metadata, '$.collections') AS collections
                FROM {DB_TABLE_ALBUMS} WHERE item_id IN :item_ids""",
            {"item_ids": library_ids},
            limit=0,
        )
        return {
            str(row["item_id"]): [
                MediaItemCollection.from_dict(entry) for entry in json_loads(row["collections"])
            ]
            for row in rows
            if row["collections"]
        }

    async def album_group_versions(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        search: str | None = None,
        sort_field: SortField | None = None,
        sort_direction: SortDirection | None = None,
        limit: int | None = 500,
        offset: int = 0,
        random_seed: int | None = None,
    ) -> list[Album]:
        """
        Return the other versions of an album that are in its own release group.

        A version whose release group cannot be determined is left out, and an album without
        a release group has no versions.

        :param item_id: The album id on the provider, or its library id.
        :param provider_instance_id_or_domain: The provider the id belongs to, or "library".
        :param search: Only list the versions whose name or artist name contains this text.
        :param sort_field: Sort field, the order they are found in (ORIGINAL) when omitted.
        :param sort_direction: Sort direction, the field's default when omitted.
        :param limit: Maximum number of versions to return; 0 returns them all.
        :param offset: Number of versions to skip.
        :param random_seed: Keep random ordering stable across pages with the same seed.
        """
        sort_field, sort_direction = resolve_listing_sort(
            ListingType.VERSIONS, sort_field, sort_direction
        )
        album = await self.get(item_id, provider_instance_id_or_domain, allow_update_metadata=False)
        lookups = _BarcodeBudget()
        if not (release_group_id := await self._resolve_release_group(album, lookups)):
            return []
        siblings = [
            sibling
            for sibling in await self._library_group_versions(release_group_id)
            if sibling.uri != album.uri
            and not album.provider_mappings.intersection(sibling.provider_mappings)
        ]
        result = list(siblings)
        for candidate in await self.versions(item_id, provider_instance_id_or_domain, limit=0):
            if any(candidate.provider_mappings & sibling.provider_mappings for sibling in siblings):
                continue
            if await self._resolve_release_group(candidate, lookups) == release_group_id:
                result.append(candidate)
        return apply_listing(
            result,
            ListingType.VERSIONS,
            search=search,
            sort_field=sort_field,
            sort_direction=sort_direction,
            limit=limit,
            offset=offset,
            random_seed=random_seed,
        )

    async def album_group_info(
        self, item_id: str, provider_instance_id_or_domain: str
    ) -> AlbumGroupInfo:
        """
        Return where an album stands within its release group in the library.

        Only a library album can be the default version of its group.

        :param item_id: The album id on the provider, or its library id.
        :param provider_instance_id_or_domain: The provider the id belongs to, or "library".
        """
        if provider_instance_id_or_domain != "library":
            return AlbumGroupInfo()
        if not (release_group_id := await self._library_release_group(int(item_id))):
            return AlbumGroupInfo()
        database = self.mass.music.database
        params: dict[str, Any] = {"release_group_id": release_group_id}
        in_library = self._provider_filter_clause(params, None, in_library_only=True)
        version_count = await database.get_count_from_query(
            f"SELECT {DB_TABLE_ALBUMS}.item_id FROM {DB_TABLE_ALBUMS} "
            f"WHERE {_RELEASE_GROUP_SQL} = :release_group_id AND {in_library}",
            params,
        )
        default_row = await database.get_row(
            DB_TABLE_ALBUM_GROUP_DEFAULTS, {"release_group_id": release_group_id}
        )
        default_item_id = str(default_row["album_id"]) if default_row else None
        is_default = default_item_id == str(item_id)
        return AlbumGroupInfo(
            release_group_id=release_group_id,
            version_count=version_count,
            default_item_id=default_item_id,
            is_default=is_default,
            can_set_default=version_count > 1 and not is_default,
        )

    async def set_album_group_default(self, item_id: str) -> None:
        """
        Make a library album the default version of its release group.

        :param item_id: The library id of the album.
        :raises InvalidDataError: When the album has no release group.
        """
        library_id = int(item_id)
        if not (release_group_id := await self._library_release_group(library_id)):
            msg = f"Album {item_id} has no release group"
            raise InvalidDataError(msg)
        await self.mass.music.database.insert_or_replace(
            DB_TABLE_ALBUM_GROUP_DEFAULTS,
            {"release_group_id": release_group_id, "album_id": library_id},
        )
        album = await self.get_library_item(library_id)
        self.mass.signal_event(EventType.MEDIA_ITEM_UPDATED, album.uri, album)

    async def album_series(
        self,
        hidden: bool = False,
        search: str | None = None,
        limit: int = 500,
        offset: int = 0,
        sort_field: SortField | None = None,
        sort_direction: SortDirection | None = None,
        random_seed: int | None = None,
    ) -> list[MediaCollection[Album]]:
        """
        Return the album series of the library, each with its albums in series order.

        :param hidden: List the hidden series instead of the shown ones.
        :param search: Only list the series whose name or one of its albums matches.
        :param limit: Maximum number of series to return; 0 returns them all.
        :param offset: Number of series to skip.
        :param sort_field: Sort field to use.
        :param sort_direction: Sort direction, the field's default when omitted.
        :param random_seed: Keep random ordering stable across pages with the same seed.
        """
        hidden_titles = await self._hidden_series_titles()
        series = [
            item
            for item in await self.album_group_items(
                search=search,
                limit=0,
                sort_field=sort_field,
                sort_direction=sort_direction,
                collapse_collections=True,
                series_filter="series",
                random_seed=random_seed,
            )
            if isinstance(item, MediaCollection) and (item.name in hidden_titles) == hidden
        ]
        return series[offset : offset + limit] if limit else series[offset:]

    async def set_album_series_hidden(self, item_id: str, hidden: bool) -> None:
        """
        Hide an album series from the series list, or show it again.

        :param item_id: The item id of the series.
        :param hidden: Hide the series (True) or show it (False).
        """
        title = get_collection_name_from_item_id(item_id)
        database = self.mass.music.database
        if hidden:
            await database.insert_or_replace(DB_TABLE_ALBUM_SERIES_HIDDEN, {"title": title})
        else:
            await database.delete(DB_TABLE_ALBUM_SERIES_HIDDEN, {"title": title})

    async def hidden_album_series(self) -> list[str]:
        """Return the item ids of the hidden album series."""
        return sorted(
            get_collection_item_id(title, MediaType.ALBUM)
            for title in await self._hidden_series_titles()
        )

    async def _hidden_series_titles(self) -> set[str]:
        """Return the titles of the hidden album series."""
        rows = await self.mass.music.database.get_rows(DB_TABLE_ALBUM_SERIES_HIDDEN, limit=0)
        return {row["title"] for row in rows}

    def _representative_clause(
        self,
        parts: list[str],
        joins: list[str],
        params: dict[str, Any],
        *,
        favorite: bool | None,
        search: str | None,
        genre: int | list[int] | None,
        provider_filter: list[str] | None,
        played_only: bool,
        reachable_via: list[str] | None,
    ) -> str:
        """
        Return the condition keeping the representative of each release group.

        The ranking sees the listing's own filters, so a group stays listed through a version
        that matches them when its usual representative does not.
        """
        candidate_parts = list(parts)
        self._apply_filters(
            query_parts=candidate_parts,
            query_params=params,
            favorite=favorite,
            search=self._preprocess_search(search),
            genre_ids=self._preprocess_genre_ids(genre),
            provider_filter=provider_filter,
            played_only=played_only,
            in_library_only=True,
            reachable_via=reachable_via,
        )
        where = " AND ".join(self._clean_query_parts(candidate_parts)) or "1"
        return f"""{DB_TABLE_ALBUMS}.item_id IN (
            SELECT ranked.item_id FROM (
                SELECT candidates.item_id, ROW_NUMBER() OVER (
                    PARTITION BY COALESCE(
                        candidates.release_group_id, 'album:' || candidates.item_id
                    )
                    ORDER BY (defaults.album_id IS NOT NULL) DESC,
                        candidates.timestamp_added DESC,
                        candidates.item_id DESC
                ) AS group_rank
                FROM (
                    SELECT DISTINCT {DB_TABLE_ALBUMS}.item_id,
                        {DB_TABLE_ALBUMS}.timestamp_added,
                        {_RELEASE_GROUP_SQL} AS release_group_id
                    FROM {DB_TABLE_ALBUMS} {" ".join(joins)}
                    WHERE {where}
                ) candidates
                LEFT JOIN {DB_TABLE_ALBUM_GROUP_DEFAULTS} defaults
                    ON defaults.release_group_id = candidates.release_group_id
                    AND defaults.album_id = candidates.item_id
            ) ranked WHERE ranked.group_rank = 1
        )"""

    async def _collapse_groups[AlbumOrSeries: Album | MediaCollection[Album]](
        self, albums: list[AlbumOrSeries]
    ) -> list[AlbumOrSeries]:
        """
        Return the albums keeping one library item per release group, in their order.

        A series (collection) passes through as it is.
        """
        library_ids = [int(album.item_id) for album in albums if _is_library_album(album)]
        if not library_ids:
            return albums
        rows = await self.mass.music.database.get_rows_from_query(
            f"""SELECT candidates.item_id, candidates.release_group_id,
                    candidates.timestamp_added,
                    defaults.album_id IS NOT NULL AS is_default
                FROM (
                    SELECT {DB_TABLE_ALBUMS}.item_id, {DB_TABLE_ALBUMS}.timestamp_added,
                        {_RELEASE_GROUP_SQL} AS release_group_id
                    FROM {DB_TABLE_ALBUMS} WHERE {DB_TABLE_ALBUMS}.item_id IN :item_ids
                ) candidates
                LEFT JOIN {DB_TABLE_ALBUM_GROUP_DEFAULTS} defaults
                    ON defaults.release_group_id = candidates.release_group_id
                    AND defaults.album_id = candidates.item_id""",
            {"item_ids": library_ids},
            limit=0,
        )
        best_of_group: dict[str, tuple[int, int, int]] = {}
        group_of_item: dict[int, str] = {}
        for row in rows:
            if not (release_group_id := row["release_group_id"]):
                continue
            group_of_item[row["item_id"]] = release_group_id
            rank = (row["is_default"], row["timestamp_added"] or 0, row["item_id"])
            best_of_group[release_group_id] = max(rank, best_of_group.get(release_group_id, rank))
        winners = {rank[2] for rank in best_of_group.values()}
        return [
            album
            for album in albums
            if not _is_library_album(album)
            or int(album.item_id) not in group_of_item
            or int(album.item_id) in winners
        ]

    async def _library_release_group(self, library_id: int) -> str | None:
        """Return the stored release group of a library album."""
        rows = await self.mass.music.database.get_rows_from_query(
            f"SELECT {_RELEASE_GROUP_SQL} AS release_group_id FROM {DB_TABLE_ALBUMS} "
            f"WHERE {DB_TABLE_ALBUMS}.item_id = :item_id",
            {"item_id": library_id},
            limit=1,
        )
        return cast("str | None", rows[0]["release_group_id"]) if rows else None

    async def _library_group_versions(self, release_group_id: str) -> list[Album]:
        """Return the library albums of a release group."""
        return await self.get_library_items_by_query(
            limit=_MAX_GROUP_VERSIONS,
            in_library_only=True,
            extra_query_parts=[f"{_RELEASE_GROUP_SQL} = :album_group_release_group"],
            extra_query_params={"album_group_release_group": release_group_id},
        )

    async def _resolve_release_group(self, album: Album, lookups: _BarcodeBudget) -> str | None:
        """Return the release group of an album, from its ids, its release or its barcode."""
        known_groups = [
            value for id_type, value in album.external_ids if id_type == ExternalID.MB_RELEASEGROUP
        ]
        if known_groups:
            return min(known_groups)
        musicbrainz = cast("MusicbrainzProvider | None", self.mass.get_provider("musicbrainz"))
        if musicbrainz is None:
            return None
        if album.mbid:
            with contextlib.suppress(*_LOOKUP_ERRORS):
                enrichment = await musicbrainz.get_version_enrichment(album.mbid)
                if enrichment and enrichment.release_group_id:
                    return enrichment.release_group_id
        barcodes = sorted(
            barcode_to_upc(value)
            for id_type, value in album.external_ids
            if id_type == ExternalID.BARCODE and is_valid_barcode(value)
        )
        groups = {
            group
            for barcode in barcodes
            if (group := await self._barcode_release_group(musicbrainz, barcode, lookups))
        }
        return groups.pop() if len(groups) == 1 else None

    async def _barcode_release_group(
        self, musicbrainz: MusicbrainzProvider, barcode: str, lookups: _BarcodeBudget
    ) -> str | None:
        """Return the one release group a barcode belongs to, None when unknown or ambiguous."""
        cache_key = f"barcode_release_group.{barcode}"
        cached = await self.mass.cache.get(cache_key, provider=_BARCODE_CACHE_PROVIDER)
        if cached is not None:
            return cast("str", cached) or None
        if not lookups.take():
            return None
        try:
            releases = await musicbrainz.get_releases_by_barcode(barcode)
        except _LOOKUP_ERRORS as err:
            self.logger.debug("MusicBrainz barcode lookup failed for %s: %s", barcode, err)
            return None
        groups = {release.release_group.id for release in releases}
        group = groups.pop() if len(groups) == 1 else ""
        await self.mass.cache.set(
            cache_key,
            group,
            expiration=_BARCODE_CACHE_EXPIRATION,
            provider=_BARCODE_CACHE_PROVIDER,
        )
        return group or None


class _BarcodeBudget:
    """How many uncached barcode lookups one listing may still make."""

    def __init__(self) -> None:
        """Start with the full budget."""
        self._remaining = _MAX_BARCODE_LOOKUPS

    def take(self) -> bool:
        """Spend one lookup, False when none is left."""
        if self._remaining <= 0:
            return False
        self._remaining -= 1
        return True


def _is_library_album(item: Album | MediaCollection[Album]) -> bool:
    """Return whether the item is a library album, not a series or a provider album."""
    return item.media_type == MediaType.ALBUM and item.provider == "library"


def _sequence_key(sequence: Any) -> tuple[int, float, str]:
    """Order the numbers in a series first, then the text ones, then the albums without one."""
    if sequence is None or sequence == "":
        return (2, 0.0, "")
    try:
        return (0, float(sequence), "")
    except TypeError, ValueError:
        return (1, 0.0, str(sequence).casefold())
