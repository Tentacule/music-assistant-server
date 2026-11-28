"""Helpers for the music videos of the fork."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType

if TYPE_CHECKING:
    from music_assistant_models.queue_item import QueueItem
    from music_assistant_models.streamdetails import StreamDetails

# input option: leave out every video stream of the source, so ffmpeg reads the audio only
FFMPEG_AUDIO_ONLY_INPUT_ARG = "-vn"


def drop_video_streams(queue_item: QueueItem, streamdetails: StreamDetails) -> None:
    """
    Make every ffmpeg run on the stream of a music video read only its audio.

    The players get the audio of a music video like a track, and the clients show the picture
    themselves. Without this, ffmpeg would decode the video, or try to encode it as cover art
    for an output format that takes one.

    :param queue_item: The queue item the stream details belong to.
    :param streamdetails: The stream details to adjust in place.
    """
    if MediaType.MUSIC_VIDEO not in (queue_item.media_type, streamdetails.media_type):
        return
    extra_input_args = list(streamdetails.extra_input_args or [])
    if FFMPEG_AUDIO_ONLY_INPUT_ARG not in extra_input_args:
        streamdetails.extra_input_args = [*extra_input_args, FFMPEG_AUDIO_ONLY_INPUT_ARG]
