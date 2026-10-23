"""Named bounded acquisition policies shared by the v3 engine and CLI."""
import hashlib
from pathlib import Path
from types import MappingProxyType

DEFAULT_POLICY = "video-only-v2"
FORMAT_SELECTORS = MappingProxyType({
    "legacy-v1": "18/b[height<=360]",
    "video-only-v2": "bv[height<=360]",
})
MAX_SEGMENT_BYTES = 800 * 1024 * 1024
MAX_FULL_SOURCE_BYTES = 4 * 1024 * 1024 * 1024
LOADED_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest().upper()


def policy_for(name=DEFAULT_POLICY):
    if name not in FORMAT_SELECTORS:
        raise ValueError("unknown acquisition policy")
    return {
        "max_segment_bytes": MAX_SEGMENT_BYTES,
        "max_full_source_bytes": MAX_FULL_SOURCE_BYTES,
        "source_format_selector": FORMAT_SELECTORS[name],
        "section_youtube_player_client": "tv_embedded",
        "full_youtube_player_client": "default",
        "native_http_chunk_size": "256K",
        "playlist_allowed": False,
        "authentication_allowed": False,
        "forced_keyframe_reencode_allowed": False,
        "publication": "atomic-create-new-only",
        "input_protocol": "https-known-youtube-host-url-via-stdin",
        "probe_protocol_whitelist": "file,crypto,data",
    }
