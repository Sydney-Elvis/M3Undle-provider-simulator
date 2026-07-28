#!/usr/bin/env python3

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import platform
import socket
import struct
import threading
import time
import urllib.request
import uuid
from importlib import metadata as importlib_metadata
from collections import deque
from dataclasses import dataclass, field, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, TextIO
from urllib.parse import parse_qs, urlparse

import sys

import jsonschema
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from playback_contract import parse_single_range, playback_media

# This engine has no dependency on the m3undle-lab orchestration toolkit
# (it used to import FIXTURES_DIR/ensure_layout from the lab's shared
# common.py -- 1113 lines of GHCR-deploy/git-checkout/srv2-sync machinery
# the simulator never used, imported wholesale into every build). Its own
# fixtures/ directory, one level up from this file, is the only thing it
# actually needs.
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def ensure_layout() -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)


DEFAULT_CONTENT_TYPE = "video/MP2T"
DEFAULT_CHUNK_COUNT = 600
DEFAULT_CHUNK_INTERVAL_MS = 100
DEFAULT_STARTUP_DELAY_MS = 0
DEFAULT_FAILURE_MODE = "none"
DEFAULT_FAILURE_AFTER_CHUNKS = 0
DEFAULT_FAILURE_AFTER_SECONDS = 0.0
DEFAULT_STALL_SECONDS = 15.0
DEFAULT_MAX_STREAMS = 2
DEFAULT_FAILURE_STATUS_CODE = 503
DEFAULT_RETRY_AFTER_SECONDS = 0
DEFAULT_PAYLOAD_MODE = "text"
DEFAULT_FILE_CHUNK_BYTES = 1316
DEFAULT_PLAYLIST_FORMAT = "direct"
DEFAULT_HLS_SEGMENT_COUNT = 6
DEFAULT_HLS_SEGMENT_CHUNKS = 10
DEFAULT_HLS_ENDLIST = True

VALID_FAILURE_MODES = frozenset({
    "none",
    "immediate_error",
    "http_error",
    "close_after_chunks",
    "close_after_seconds",
    "stall_after_chunks",
    "disconnect_before_headers",
    "reset_before_headers",
})
VALID_PAYLOAD_MODES = frozenset({"text", "ts", "file"})
VALID_PLAYLIST_FORMATS = frozenset({"direct", "hls"})
DEFAULT_TS_VARIANT = "idr"
VALID_TS_VARIANTS = frozenset({
    "idr", "no_idr", "continuity_counter_fault", "delayed_pat_pmt", "malformed_packet",
})
MAX_JSON_BODY_BYTES = 65_536

# Engine release version. Compared against a scenario's minimum_engine_version
# at load time (Stage A5 versioning decision: schema_version and
# minimum_engine_version are distinct -- schema_version is the file-format
# version this engine understands, minimum_engine_version is a release floor).
ENGINE_VERSION = (0, 2, 0)
# schema_version values this engine's loader understands. Absent entirely
# (legacy flat fixture, predates the field) is permitted -- only a present
# but unsupported value is rejected.
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

EVENT_SCHEMA_VERSION = 1

# Single source of truth for the ScenarioSequenceRunner's known vocabulary --
# shared between runtime execution (_apply_action/_resolve_trigger) and the
# fail-closed load-time validator (validate_scenario_config) so the two can
# never drift apart.
VALID_SEQUENCE_ACTIONS = frozenset({"set_channel_behavior", "wait", "switch_source"})
VALID_TRIGGER_AFTER_KEYS = frozenset({"seconds", "connections", "chunks", "bytes"})
ALLOWED_OVERRIDE_FIELDS = frozenset({
    "failure_mode",
    "failure_status_code",
    "retry_after_seconds",
    "startup_delay_ms",
    "stall_seconds",
    "ts_variant",
    "chunk_interval_ms",
})
# switch_source's fields: distinct from ALLOWED_OVERRIDE_FIELDS on purpose.
# set_channel_behavior's overrides only take effect on a channel's *next*
# connection (behavior is resolved once per connection, at connection-open
# time -- see handle_channel_stream()); several existing scenarios
# (TS-SAFE-03, the CLEAN-RELAY-05 reimplementation) depend on that
# next-connection-only semantic and must not change. switch_source is the
# new, narrower, opt-in mechanism for Stage A7 baseline #13 (midstream
# source replacement): it mutates only payload-generation fields, applied
# live to already-open connections on the target channel (checked once per
# chunk) as well as to new ones, without touching failure/timing fields or
# altering set_channel_behavior's existing contract at all.
ALLOWED_LIVE_SOURCE_FIELDS = frozenset({
    "payload_mode",
    "payload_file",
    "ts_variant",
    "payload",
    "payload_chunk_bytes",
})

# ---------------------------------------------------------------------------
# MPEG-TS binary packet generation
# ---------------------------------------------------------------------------
# Produces a repeating 5-packet sequence: PAT, PMT, SPS video, PPS video, IDR video.
# The M3Undle scanner will detect H264Idr safe-start after the first complete cycle.

_TS_PACKET_SIZE = 188


def _ts_packet(pid: int, payload: bytes, *, payload_unit_start: bool = False, continuity_counter: int = 0) -> bytes:
    pkt = bytearray(b"\xff" * _TS_PACKET_SIZE)
    pkt[0] = 0x47
    pkt[1] = (0x40 if payload_unit_start else 0x00) | ((pid >> 8) & 0x1F)
    pkt[2] = pid & 0xFF
    pkt[3] = 0x10 | (continuity_counter & 0x0F)  # adaptation_field_control=payload-only, CC
    data = payload[: _TS_PACKET_SIZE - 4]
    pkt[4 : 4 + len(data)] = data
    return bytes(pkt)


_PAT_PAYLOAD = bytes([
    0x00,                          # pointer_field
    0x00,                          # table_id (PAT)
    0xB0, 0x0D,                    # section_length = 13
    0x00, 0x01,                    # transport_stream_id
    0xC1, 0x00, 0x00,              # version/section/last_section
    0x00, 0x01,                    # program_number = 1
    0xE0, 0x64,                    # PMT PID = 100
    0x00, 0x00, 0x00, 0x00,        # CRC32 (not verified)
])

_PMT_PAYLOAD = bytes([
    0x00,                          # pointer_field
    0x02,                          # table_id (PMT)
    0xB0, 0x12,                    # section_length = 18
    0x00, 0x01,                    # program_number
    0xC1, 0x00, 0x00,              # version/section/last_section
    0xE1, 0x00,                    # PCR PID = 256
    0xF0, 0x00,                    # program_info_length = 0
    0x1B,                          # stream_type = H.264
    0xE1, 0x00,                    # elementary PID = 256
    0xF0, 0x00,                    # ES_info_length = 0
    0x00, 0x00, 0x00, 0x00,        # CRC32 (not verified)
])

_PES_HEADER = bytes([0x00, 0x00, 0x01, 0xE0, 0x00, 0x00, 0x80, 0x80, 0x00])

_SPS_NAL = bytes([0x00, 0x00, 0x01, 0x67, 0x42, 0x00])
_PPS_NAL = bytes([0x00, 0x00, 0x01, 0x68, 0x01, 0x00])
_IDR_NAL = bytes([0x00, 0x00, 0x01, 0x65, 0x00, 0x00])
_NON_IDR_NAL = bytes([0x00, 0x00, 0x01, 0x01, 0x00, 0x00])

_TS_SEQUENCE: tuple[bytes, ...] = (
    _ts_packet(0,   _PAT_PAYLOAD, payload_unit_start=True),
    _ts_packet(100, _PMT_PAYLOAD, payload_unit_start=True),
    _ts_packet(256, _PES_HEADER + _SPS_NAL, payload_unit_start=True),
    _ts_packet(256, _PES_HEADER + _PPS_NAL, payload_unit_start=True),
    _ts_packet(256, _PES_HEADER + _IDR_NAL, payload_unit_start=True),
)

# Same PAT/PMT/SPS/PPS cadence, but the fifth packet is a non-IDR coded slice
# (H.264 NAL type 0x01, not 0x05) instead of an IDR -- exercises M3Undle's
# byte-count-based FallbackPacketBoundary safe-start path, which a stream
# that always offers an IDR within the first cycle never reaches.
_TS_SEQUENCE_NO_IDR: tuple[bytes, ...] = (
    _ts_packet(0,   _PAT_PAYLOAD, payload_unit_start=True),
    _ts_packet(100, _PMT_PAYLOAD, payload_unit_start=True),
    _ts_packet(256, _PES_HEADER + _SPS_NAL, payload_unit_start=True),
    _ts_packet(256, _PES_HEADER + _PPS_NAL, payload_unit_start=True),
    _ts_packet(256, _PES_HEADER + _NON_IDR_NAL, payload_unit_start=True),
)

# ---------------------------------------------------------------------------
# Stage A7 baseline #10/#11/#12 -- dynamic TS fault variants
# ---------------------------------------------------------------------------
# Unlike _TS_SEQUENCE/_TS_SEQUENCE_NO_IDR (precomputed once, at import time,
# and left completely untouched by everything below), these three ts_variant
# values are generated per chunk_index, on demand, because each needs to
# know its position in the stream to decide when its fault fires. Each is a
# single, discrete, deterministic event at a fixed point (matching how a
# real transient TS fault is normally observed and logged once, not an
# ongoing broken condition) -- deliberately not configurable, the same
# no-knobs precedent _TS_SEQUENCE_NO_IDR already set. A scenario gets
# fault-timing control for free via the existing switch_source/
# set_channel_behavior + trigger mechanism (switch ts_variant onto one of
# these mid-stream), not by parameterizing the fault itself.
TS_CONTINUITY_FAULT_AFTER_VIDEO_PACKETS = 10
TS_DELAYED_PAT_PMT_CYCLES = 3
TS_MALFORMED_PACKET_AFTER_CHUNKS = 20
TS_DYNAMIC_FAULT_VARIANTS = frozenset({
    "continuity_counter_fault",
    "delayed_pat_pmt",
    "malformed_packet",
})


def _ts_video_pid_occurrences_before(chunk_index: int) -> int:
    """
    How many times PID 256 (video) has appeared at indices < chunk_index in
    the fixed 5-packet PAT/PMT/SPS/PPS/(IDR|non-IDR) cycle -- i.e. this
    chunk_index's own 0-indexed occurrence number when it lands on a video
    position (chunk_index % 5 in {2, 3, 4}). Pure function of chunk_index,
    no per-connection state needed.
    """
    return (chunk_index // 5) * 3 + max(0, (chunk_index % 5) - 2)


def _ts_dynamic_packet(ts_variant: str, chunk_index: int) -> tuple[bytes, str | None]:
    """
    Generates one packet for TS_DYNAMIC_FAULT_VARIANTS on the fly. Returns
    (packet_bytes, fault_event_name); fault_event_name is None except on the
    exact chunk_index where a fault's single discrete event fires -- the
    caller logs it via state.log_event(), keeping this function pure/
    testable while still giving scenarios (and M3Undle-side test
    assertions) a queryable moment the fault actually happened, per the Q2
    media-fault domain review's "the engine must log what it emitted"
    discipline.
    """
    position = chunk_index % 5
    fault_event: str | None = None

    if ts_variant == "delayed_pat_pmt" and chunk_index < TS_DELAYED_PAT_PMT_CYCLES * 5 and position in (0, 1):
        # Withhold PAT/PMT for the configured number of cycles -- substitute
        # an extra IDR packet so the client sees video data with no program
        # tables yet, the real "provider took a while to signal PAT/PMT"
        # condition baseline #12 describes.
        packet = _ts_packet(256, _PES_HEADER + _IDR_NAL, payload_unit_start=True)
    elif position == 0:
        packet = _ts_packet(0, _PAT_PAYLOAD, payload_unit_start=True)
        if ts_variant == "delayed_pat_pmt" and chunk_index == TS_DELAYED_PAT_PMT_CYCLES * 5:
            fault_event = "ts_pat_pmt_delay_ended"
    elif position == 1:
        packet = _ts_packet(100, _PMT_PAYLOAD, payload_unit_start=True)
    else:
        nal = {2: _SPS_NAL, 3: _PPS_NAL, 4: _IDR_NAL}[position]
        cc = _ts_video_pid_occurrences_before(chunk_index)
        if ts_variant == "continuity_counter_fault" and cc >= TS_CONTINUITY_FAULT_AFTER_VIDEO_PACKETS:
            if cc == TS_CONTINUITY_FAULT_AFTER_VIDEO_PACKETS:
                fault_event = "ts_continuity_discontinuity_injected"
            cc += 1  # a single skipped count -- one discrete discontinuity,
            # then normal incrementing continues from the new value, not a
            # permanently broken counter.
        packet = _ts_packet(256, _PES_HEADER + nal, payload_unit_start=True, continuity_counter=cc & 0x0F)

    if ts_variant == "malformed_packet" and chunk_index == TS_MALFORMED_PACKET_AFTER_CHUNKS:
        # transport_error_indicator (byte 1, bit 0x80) -- the MPEG-TS spec's
        # own defined signal for "this packet is known-bad" (what a
        # satellite/cable receiver sets when forward error correction
        # fails). One well-defined interpretation of "malformed," not an
        # exhaustive taxonomy of every possible corruption -- small v1,
        # matching every other fault built this session.
        mutable = bytearray(packet)
        mutable[1] |= 0x80
        packet = bytes(mutable)
        fault_event = "ts_malformed_packet_injected"

    return packet, fault_event


def _resolve_payload_file(payload_file: str) -> Path:
    requested = Path(payload_file)
    candidates = [requested]
    if not requested.is_absolute():
        candidates = [FIXTURES_DIR.parent / requested, FIXTURES_DIR / requested, requested]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise SystemExit(f"Payload file not found: {payload_file}")


# mtime-validated cache of payload-file bytes. The streaming loop used to
# call Path.read_bytes() on every single chunk (a 30s/~3.5MB fixture at 50
# chunks/s meant ~175MB/s of redundant disk reads per connection). The
# mtime_ns check preserves the old behavior that mattered: editing the file
# on disk mid-run still takes effect at the next chunk boundary. Worst case
# under concurrent access is a duplicate read, never a stale one.
_PAYLOAD_FILE_CACHE: dict[str, tuple[int, bytes]] = {}


def _read_payload_file(path_str: str) -> bytes:
    mtime_ns = Path(path_str).stat().st_mtime_ns
    cached = _PAYLOAD_FILE_CACHE.get(path_str)
    if cached is not None and cached[0] == mtime_ns:
        return cached[1]
    data = Path(path_str).read_bytes()
    _PAYLOAD_FILE_CACHE[path_str] = (mtime_ns, data)
    return data


def _file_payload_chunk(payload: bytes, chunk_index: int, chunk_bytes: int) -> bytes:
    start = (chunk_index * chunk_bytes) % len(payload)
    chunk = payload[start : start + chunk_bytes]
    if len(chunk) < chunk_bytes:
        chunk += payload[: chunk_bytes - len(chunk)]
    return chunk


def _validate_override_body(body: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """
    Shared by ProviderState.set_channel_override() (the live /debug HTTP
    endpoint) and validate_scenario_config() (fail-closed, at load time, for
    set_channel_behavior sequence steps) -- one canonical implementation
    rather than the same field-by-field checks drifting between a runtime
    path and a load-time path, per this plan's repeated "one canonical
    implementation" guardrail. Returns (override_dict, error); error is None
    on success.
    """
    unknown = sorted(set(body) - ALLOWED_OVERRIDE_FIELDS)
    if unknown:
        return {}, f"unknown override field(s): {', '.join(unknown)}"

    override: dict[str, Any] = {}
    if "failure_mode" in body:
        failure_mode = str(body["failure_mode"])
        if failure_mode not in VALID_FAILURE_MODES:
            return {}, f"unknown failure_mode={failure_mode!r}"
        override["failure_mode"] = failure_mode
    if "ts_variant" in body:
        ts_variant = str(body["ts_variant"])
        if ts_variant not in VALID_TS_VARIANTS:
            return {}, f"unknown ts_variant={ts_variant!r}"
        override["ts_variant"] = ts_variant
    for field_name in ("failure_status_code", "retry_after_seconds", "startup_delay_ms", "chunk_interval_ms"):
        if field_name in body:
            try:
                value = int(body[field_name])
            except (TypeError, ValueError):
                return {}, f"{field_name} must be an integer"
            if value < 0:
                return {}, f"{field_name} must be greater than or equal to zero"
            override[field_name] = value
    if "stall_seconds" in body:
        try:
            stall_seconds = float(body["stall_seconds"])
        except (TypeError, ValueError):
            return {}, "stall_seconds must be numeric"
        if stall_seconds < 0:
            return {}, "stall_seconds must be greater than or equal to zero"
        override["stall_seconds"] = stall_seconds
    return override, None


def _validate_live_source_body(body: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """
    Shared by ProviderState.set_live_source() (applied live, per chunk, to
    already-open connections -- the switch_source action) and
    validate_scenario_config() (fail-closed, at load time). Same "one
    canonical implementation" discipline as _validate_override_body(), for
    the narrower ALLOWED_LIVE_SOURCE_FIELDS set. payload_file is resolved
    (and so validated to exist) eagerly here, matching build_behavior()'s
    existing eager _resolve_payload_file() call at load time -- a
    mid-connection source switch to a missing file should fail immediately,
    not partway through streaming it.
    """
    unknown = sorted(set(body) - ALLOWED_LIVE_SOURCE_FIELDS)
    if unknown:
        return {}, f"unknown switch_source field(s): {', '.join(unknown)}"

    override: dict[str, Any] = {}
    if "payload_mode" in body:
        payload_mode = str(body["payload_mode"])
        if payload_mode not in VALID_PAYLOAD_MODES:
            return {}, f"unknown payload_mode={payload_mode!r}"
        override["payload_mode"] = payload_mode
    if "ts_variant" in body:
        ts_variant = str(body["ts_variant"])
        if ts_variant not in VALID_TS_VARIANTS:
            return {}, f"unknown ts_variant={ts_variant!r}"
        override["ts_variant"] = ts_variant
    if "payload_file" in body:
        payload_file = str(body["payload_file"])
        _resolve_payload_file(payload_file)  # raises SystemExit if missing -- fail closed
        override["payload_file"] = payload_file
    if "payload_chunk_bytes" in body:
        try:
            payload_chunk_bytes = int(body["payload_chunk_bytes"])
        except (TypeError, ValueError):
            return {}, "payload_chunk_bytes must be an integer"
        if payload_chunk_bytes <= 0:
            return {}, "payload_chunk_bytes must be greater than zero"
        override["payload_chunk_bytes"] = payload_chunk_bytes
    if "payload" in body:
        override["payload"] = str(body["payload"])
    return override, None


@dataclass(frozen=True)
class ChannelBehavior:
    channel_id: str
    content_type: str
    chunk_count: int
    chunk_interval_ms: int
    startup_delay_ms: int
    failure_mode: str
    failure_after_chunks: int
    failure_after_seconds: float
    stall_seconds: float
    failure_status_code: int
    retry_after_seconds: int
    payload: str
    payload_mode: str = "text"
    ts_variant: str = DEFAULT_TS_VARIANT
    payload_file: str = ""
    payload_chunk_bytes: int = DEFAULT_FILE_CHUNK_BYTES
    playlist_format: str = DEFAULT_PLAYLIST_FORMAT
    hls_segment_count: int = DEFAULT_HLS_SEGMENT_COUNT
    hls_segment_chunks: int = DEFAULT_HLS_SEGMENT_CHUNKS
    hls_endlist: bool = DEFAULT_HLS_ENDLIST
    display_name: str = ""
    group_title: str = ""
    tvg_id: str = ""


@dataclass
class XtreamConfig:
    """Xtream Codes API surface configuration loaded from the fixture."""
    username: str = "testuser"
    password: str = "testpass"
    # categories: list of {"category_id": str, "category_name": str}
    categories: list[dict[str, Any]] = field(default_factory=list)
    # stream_ids: mapping of channel_id → stream_id (int); auto-generated if absent
    stream_ids: dict[str, int] = field(default_factory=dict)
    vod_categories: list[dict[str, Any]] = field(default_factory=list)
    vod_streams: list[dict[str, Any]] = field(default_factory=list)
    series_categories: list[dict[str, Any]] = field(default_factory=list)
    series: list[dict[str, Any]] = field(default_factory=list)
    series_info: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class EpgConfig:
    """XMLTV/EPG configuration loaded from the fixture."""
    # channels: list of {"id": str, "name": str, "icon": str}
    channels: list[dict[str, Any]] = field(default_factory=list)
    # programmes: list of XMLTV programme dicts
    programmes: list[dict[str, Any]] = field(default_factory=list)
    # path at which the guide is served (default: /epg/guide.xml)
    path: str = "/epg/guide.xml"


VALID_AUTH_MODES = frozenset({"static"})


@dataclass
class AuthConfig:
    """
    Generic (non-Xtream) provider path credential check -- Stage A7 baseline
    #15. This is the `provider.authentication` block the Stage A5 example
    schema showed from the start but never actually implemented (see the
    plan's coverage matrix). Distinct from XtreamConfig's username/password,
    which already gates the separate Xtream API surface -- this gates
    /playlist.m3u and /channels/{id}, the plain M3U path every other
    baseline scenario streams over.
    """
    mode: str = "static"
    username: str = "test"
    password: str = "test"


class ProviderState:
    def __init__(
        self,
        provider_name: str,
        max_streams: int,
        channels: dict[str, ChannelBehavior],
        epg: EpgConfig | None = None,
        xtream: XtreamConfig | None = None,
        auth: AuthConfig | None = None,
        fixture_path: Path | None = None,
        sequence: list[dict[str, Any]] | None = None,
        scenario_id: str | None = None,
        run_id: str | None = None,
        expected_simulator_events: list[str | dict[str, Any]] | None = None,
        run_config: dict[str, Any] | None = None,
    ) -> None:
        self.provider_name = provider_name
        self.max_streams = max_streams
        self.channels = channels
        self.epg = epg
        self.xtream = xtream
        self.auth = auth
        self.fixture_path = fixture_path
        self.sequence = sequence or []
        # The versioned event API's stable scenario_id -- the scenario file's
        # own `name:` (falls back to the provider name for legacy fixtures,
        # which never had one), not the provider_name (a provider can be
        # reloaded/renamed independently of which scenario is running).
        self.scenario_id = scenario_id or provider_name
        # Stage A8.5: engine-generated unless a caller (CI) supplies one for
        # correlation -- the one requirement-10 envelope field that was still
        # missing. Included in every event, not just --run mode's.
        self.run_id = run_id or uuid.uuid4().hex
        # Stage A8.5: expected_simulator_events/run (max_duration_s,
        # trigger_timeout_s) were parsed by _normalize_config and then
        # discarded -- load_scenario() now threads them through so --run can
        # actually check the former and honor the latter.
        self.expected_simulator_events = list(expected_simulator_events or [])
        self.run_config = dict(run_config or {})
        # Optional JSONL durability sink (--events-file), set post-construction
        # via set_events_sink() once the caller has decided where -- kept
        # separate from the bounded 200-event `events` deque, which stays a
        # /debug/state convenience, not the durable record.
        self._events_sink: TextIO | None = None
        # Per-connection timeline (Stage A8.5 result-artifact requirement):
        # first/last chunk index served and whether a file-payload loop
        # wraparound was observed, keyed by connection_id.
        self.connection_timeline: dict[str, dict[str, Any]] = {}
        self._event_seq = 0
        self._start_monotonic = time.monotonic()
        self.active_streams = 0
        self.active_by_channel: dict[str, int] = {channel_id: 0 for channel_id in channels}
        # Cumulative accepted opens per channel, never decremented -- the
        # ScenarioSequenceRunner's `after: {connections: N}` trigger counts
        # against this, not against stream_opened entries in the bounded
        # `events` deque (maxlen 200): under a reconnect storm the deque
        # rotates and an event-scan undercounts, so a trigger could silently
        # never fire.
        self.opened_by_channel: dict[str, int] = {channel_id: 0 for channel_id in channels}
        self.chunks_sent_by_channel: dict[str, int] = {channel_id: 0 for channel_id in channels}
        self.bytes_sent_by_channel: dict[str, int] = {channel_id: 0 for channel_id in channels}
        self.total_opened = 0
        self.total_closed = 0
        self.total_rejected = 0
        self.total_broken = 0
        self.healthy = True
        self.channel_overrides: dict[str, dict[str, Any]] = {}
        # switch_source's live state -- distinct from channel_overrides
        # (which only affects a channel's *next* connection). Applied both
        # to newly-resolved behavior (via _behavior_for_channel_locked) and,
        # for already-open connections, checked live once per chunk in
        # handle_channel_stream() via current_live_source(). See
        # ALLOWED_LIVE_SOURCE_FIELDS for why this is a separate mechanism.
        self.live_source_overrides: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.events: deque[dict[str, Any]] = deque(maxlen=200)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "provider_name": self.provider_name,
                "healthy": self.healthy,
                "max_streams": self.max_streams,
                "active_streams": self.active_streams,
                "active_by_channel": dict(self.active_by_channel),
                "opened_by_channel": dict(self.opened_by_channel),
                "total_opened": self.total_opened,
                "total_closed": self.total_closed,
                "total_rejected": self.total_rejected,
                "total_broken": self.total_broken,
                "channels": sorted(self.channels),
                "channel_overrides": dict(self.channel_overrides),
                "effective_channels": {
                    channel_id: self._behavior_snapshot_locked(self._behavior_for_channel_locked(channel_id))
                    for channel_id in sorted(self.channels)
                },
                "connection_timeline": dict(self.connection_timeline),
                "recent_events": list(self.events),
            }

    def log_event(self, event: str, **fields: Any) -> None:
        """
        Every event gets the full versioned-API envelope (event_schema_version,
        scenario_id, run_id, a monotonic sequence number, virtual_time_ms, and
        an action_id slot) regardless of whether the caller passed anything --
        Stage A5's "event log is a versioned public API" decision. action_id
        defaults to None and stays None for organic engine events; it's only
        non-null when the event was actually produced by executing a
        ScenarioSequenceRunner step (the caller passes action_id= explicitly
        in that case, which overrides the None default below since ** always
        wins over the literal it's spread after).
        """
        with self.lock:
            self._event_seq += 1
            entry = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "provider": self.provider_name,
                "scenario_id": self.scenario_id,
                "run_id": self.run_id,
                "event": event,
                "event_schema_version": EVENT_SCHEMA_VERSION,
                "sequence": self._event_seq,
                "virtual_time_ms": int((time.monotonic() - self._start_monotonic) * 1000),
                "action_id": None,
                **fields,
            }
            self.events.append(entry)
            sink = self._events_sink
        print(json.dumps(entry), flush=True)
        if sink is not None:
            sink.write(json.dumps(entry) + "\n")
            sink.flush()

    def set_events_sink(self, sink: TextIO | None) -> None:
        """Stage A8.5 event-log durability: every event logged after this
        call is also appended as a JSONL line to `sink`, in addition to the
        bounded 200-event `events` deque /debug/state exposes. Caller owns
        opening/closing the file."""
        with self.lock:
            self._events_sink = sink

    def reset_metrics(self) -> None:
        with self.lock:
            self.active_streams = 0
            self.active_by_channel = {channel_id: 0 for channel_id in self.channels}
            self.opened_by_channel = {channel_id: 0 for channel_id in self.channels}
            self.chunks_sent_by_channel = {channel_id: 0 for channel_id in self.channels}
            self.bytes_sent_by_channel = {channel_id: 0 for channel_id in self.channels}
            self.total_opened = 0
            self.total_closed = 0
            self.total_rejected = 0
            self.total_broken = 0
            self.channel_overrides.clear()
            self.live_source_overrides.clear()
            self.connection_timeline.clear()
            self.events.clear()
            self._event_seq = 0
            self._start_monotonic = time.monotonic()
        self.log_event("metrics_reset")

    def set_health(self, healthy: bool) -> None:
        with self.lock:
            self.healthy = healthy
        self.log_event("health_changed", healthy=healthy)

    def reload_fixture(self, public_base_url: str) -> str | None:
        """
        Reload channels (and EPG/Xtream config) from the fixture file on disk.
        Returns an error string on failure, or None on success.
        """
        if self.fixture_path is None:
            return "no fixture_path recorded"
        try:
            new_state = load_scenario(self.fixture_path)
        except Exception as exc:
            return str(exc)
        with self.lock:
            self.channels = new_state.channels
            self.epg = new_state.epg
            self.xtream = new_state.xtream
            self.auth = new_state.auth
            self.active_by_channel = {channel_id: 0 for channel_id in new_state.channels}
        self.log_event("fixture_reloaded", fixture=str(self.fixture_path))
        return None

    def behavior_for_channel(self, channel_id: str) -> ChannelBehavior | None:
        with self.lock:
            return self._behavior_for_channel_locked(channel_id)

    def set_channel_override(
        self, channel_id: str, body: dict[str, Any], *, action_id: str | None = None
    ) -> tuple[bool, str | None]:
        if channel_id not in self.channels:
            return False, f"unknown channel: {channel_id}"

        override, error = _validate_override_body(body)
        if error is not None:
            return False, error

        with self.lock:
            self.channel_overrides[channel_id] = override
        self.log_event(
            "channel_override_changed", channel=channel_id, override=override, action_id=action_id
        )
        return True, None

    def set_live_source(
        self, channel_id: str, body: dict[str, Any], *, action_id: str | None = None
    ) -> tuple[bool, str | None]:
        """
        Stage A7 baseline #13 (midstream source replacement): switches the
        payload an already-open connection on this channel serves, without
        waiting for reconnect. New connections opened after this call also
        pick it up (via _behavior_for_channel_locked); connections already
        open see it at their next chunk boundary (via current_live_source(),
        called from the streaming loop in handle_channel_stream()).
        """
        if channel_id not in self.channels:
            return False, f"unknown channel: {channel_id}"

        override, error = _validate_live_source_body(body)
        if error is not None:
            return False, error

        with self.lock:
            self.live_source_overrides[channel_id] = override
        self.log_event(
            "source_switched", channel=channel_id, override=override, action_id=action_id
        )
        return True, None

    def current_live_source(self, channel_id: str) -> dict[str, Any] | None:
        with self.lock:
            override = self.live_source_overrides.get(channel_id)
            return dict(override) if override else None

    def _behavior_for_channel_locked(self, channel_id: str) -> ChannelBehavior | None:
        behavior = self.channels.get(channel_id)
        if behavior is None:
            return None
        override = self.channel_overrides.get(channel_id)
        if override:
            behavior = replace(behavior, **override)
        live_source = self.live_source_overrides.get(channel_id)
        if live_source:
            behavior = replace(behavior, **live_source)
        return behavior

    @staticmethod
    def _behavior_snapshot_locked(behavior: ChannelBehavior | None) -> dict[str, Any] | None:
        if behavior is None:
            return None
        return {
            "failure_mode": behavior.failure_mode,
            "failure_status_code": behavior.failure_status_code,
            "retry_after_seconds": behavior.retry_after_seconds,
            "startup_delay_ms": behavior.startup_delay_ms,
            "stall_seconds": behavior.stall_seconds,
            "payload_mode": behavior.payload_mode,
            "ts_variant": behavior.ts_variant,
            "payload_file": behavior.payload_file or None,
            "playlist_format": behavior.playlist_format,
            "hls_segment_count": behavior.hls_segment_count,
            "hls_segment_chunks": behavior.hls_segment_chunks,
            "hls_endlist": behavior.hls_endlist,
        }

    def try_open_stream(self, channel_id: str) -> tuple[bool, dict[str, int], str]:
        with self.lock:
            if not self.healthy:
                self.total_rejected += 1
                return False, self._counts_locked(channel_id), "provider_unhealthy"
            if self.active_streams >= self.max_streams:
                self.total_rejected += 1
                return False, self._counts_locked(channel_id), "max_streams_reached"
            self.active_streams += 1
            self.active_by_channel[channel_id] = self.active_by_channel.get(channel_id, 0) + 1
            self.opened_by_channel[channel_id] = self.opened_by_channel.get(channel_id, 0) + 1
            self.total_opened += 1
            return True, self._counts_locked(channel_id), "accepted"

    def record_chunk_sent(
        self,
        channel_id: str,
        num_bytes: int,
        *,
        connection_id: str | None = None,
        chunk_index: int | None = None,
        wrapped: bool = False,
    ) -> None:
        with self.lock:
            self.chunks_sent_by_channel[channel_id] = self.chunks_sent_by_channel.get(channel_id, 0) + 1
            self.bytes_sent_by_channel[channel_id] = self.bytes_sent_by_channel.get(channel_id, 0) + num_bytes
            if connection_id is not None:
                timeline = self.connection_timeline.setdefault(connection_id, {
                    "channel": channel_id,
                    "first_chunk_index": chunk_index,
                    "last_chunk_index": chunk_index,
                    "bytes": 0,
                    "wraparound_detected": False,
                })
                timeline["last_chunk_index"] = chunk_index
                timeline["bytes"] += num_bytes
                if wrapped:
                    timeline["wraparound_detected"] = True

    def close_stream(self, channel_id: str, *, broken: bool) -> dict[str, int]:
        with self.lock:
            self.active_streams = max(0, self.active_streams - 1)
            self.active_by_channel[channel_id] = max(0, self.active_by_channel.get(channel_id, 0) - 1)
            self.total_closed += 1
            if broken:
                self.total_broken += 1
            return self._counts_locked(channel_id)

    def _counts_locked(self, channel_id: str) -> dict[str, int]:
        return {
            "active_streams": self.active_streams,
            "active_channel_streams": self.active_by_channel.get(channel_id, 0),
            "max_streams": self.max_streams,
        }


class ProviderServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        state: ProviderState,
        public_base_url: str,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.state = state
        self.public_base_url = public_base_url.rstrip("/")


class ProviderHandler(BaseHTTPRequestHandler):
    server: ProviderServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/health":
            self.handle_health()
            return
        if path == "/debug/state":
            self.write_json(HTTPStatus.OK, self.server.state.snapshot())
            return
        if path == "/playlist.m3u":
            self.handle_playlist(query)
            return
        if path.startswith("/channels/"):
            if path.endswith("/index.m3u8"):
                channel_id = path.removeprefix("/channels/").removesuffix("/index.m3u8")
                self.handle_hls_manifest(channel_id)
                return
            if "/segments/" in path and path.endswith(".ts"):
                channel_part, segment_part = path.removeprefix("/channels/").split("/segments/", 1)
                segment_id = segment_part.removesuffix(".ts")
                self.handle_hls_segment(channel_part, segment_id)
                return
            channel_id = path.rsplit("/", 1)[-1]
            self.handle_channel_stream(channel_id, query)
            return
        # EPG / XMLTV
        epg = self.server.state.epg
        if epg and path == epg.path:
            self.handle_epg()
            return
        # Xtream Codes API
        if path == "/player_api.php":
            self.handle_xtream_api(query)
            return
        if path == "/get.php":
            self.handle_xtream_get_m3u(query)
            return
        # Xtream path-credential streams: /live/{user}/{pass}/{streamId}[.ts]
        if path.startswith("/live/"):
            self.handle_xtream_stream(path, query)
            return
        if path.startswith("/movie/") or path.startswith("/series/"):
            self.handle_xtream_on_demand(path)
            return
        self.write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/debug/reset":
            self.server.state.reset_metrics()
            self.write_json(HTTPStatus.OK, {"status": "reset"})
            return
        if parsed.path == "/debug/health":
            body = self.read_json_body()
            healthy_raw = body.get("healthy", True)
            if not isinstance(healthy_raw, bool):
                self.write_json(HTTPStatus.BAD_REQUEST, {"error": "'healthy' must be a JSON boolean"})
                return
            self.server.state.set_health(healthy_raw)
            self.write_json(HTTPStatus.OK, {"healthy": healthy_raw})
            return
        if parsed.path == "/debug/reload":
            err = self.server.state.reload_fixture(self.server.public_base_url)
            if err:
                self.write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": err})
            else:
                self.write_json(HTTPStatus.OK, {"status": "reloaded"})
            return
        if parsed.path.startswith("/debug/channels/") and parsed.path.endswith("/override"):
            channel_id = parsed.path.removeprefix("/debug/channels/").removesuffix("/override").strip("/")
            body = self.read_json_body()
            ok, err = self.server.state.set_channel_override(channel_id, body)
            if not ok:
                status = HTTPStatus.NOT_FOUND if err and err.startswith("unknown channel") else HTTPStatus.BAD_REQUEST
                self.write_json(status, {"error": err or "override_failed"})
                return
            state = self.server.state.snapshot()
            self.write_json(HTTPStatus.OK, {
                "channel": channel_id,
                "override": state["channel_overrides"].get(channel_id, {}),
                "effective_behavior": state["effective_channels"].get(channel_id),
            })
            return
        self.write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    # ------------------------------------------------------------------
    # Original handlers
    # ------------------------------------------------------------------

    def handle_health(self) -> None:
        snapshot = self.server.state.snapshot()
        status = HTTPStatus.OK if snapshot["healthy"] else HTTPStatus.SERVICE_UNAVAILABLE
        self.write_json(status, {"provider": self.server.state.provider_name, "healthy": snapshot["healthy"]})

    def _provider_auth_ok(self, query: dict[str, list[str]]) -> bool:
        """
        Stage A7 baseline #15: the generic (non-Xtream) provider path's
        credential check -- Stage A5's example schema showed a
        `provider.authentication` block from the start, but it was never
        wired up until now (see the plan's coverage matrix). Mirrors
        _xtream_auth_ok()'s query-param convention exactly, gating a
        different surface (/playlist.m3u, /channels/{id}) than Xtream's.
        No `state.auth` configured means no check -- every existing
        fixture/scenario that doesn't set `provider.authentication` behaves
        identically to before this was added.
        """
        auth = self.server.state.auth
        if auth is None:
            return True
        username = (query.get("username") or [""])[0]
        password = (query.get("password") or [""])[0]
        return username == auth.username and password == auth.password

    def handle_playlist(self, query: dict[str, list[str]]) -> None:
        state = self.server.state
        if not self._provider_auth_ok(query):
            state.log_event("playlist_unauthorized")
            self.write_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid_credentials"})
            return
        base = self.server.public_base_url
        # When authentication is configured, embed the same credentials into
        # each stream URL -- a client that authenticated for the playlist
        # gets working channel URLs, matching how the Xtream surface already
        # embeds username/password into its stream URLs.
        auth_suffix = f"?username={state.auth.username}&password={state.auth.password}" if state.auth else ""
        lines: list[str] = ["#EXTM3U"]
        for channel_id, behavior in state.channels.items():
            display_name = behavior.display_name or channel_id
            group_title = behavior.group_title or state.provider_name
            tvg_id = behavior.tvg_id or channel_id
            if behavior.playlist_format == "hls":
                stream_url = f"{base}/channels/{channel_id}/index.m3u8"
            else:
                stream_url = f"{base}/channels/{channel_id}{auth_suffix}"
            lines.append(
                f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{display_name}" group-title="{group_title}",{display_name}'
            )
            lines.append(stream_url)
        payload = "\n".join(lines).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-mpegurl")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def handle_hls_manifest(self, channel_id: str) -> None:
        state = self.server.state
        behavior = state.behavior_for_channel(channel_id)
        if behavior is None:
            state.log_event("hls_manifest_unknown_channel", channel=channel_id)
            self.write_json(HTTPStatus.NOT_FOUND, {"error": "unknown_channel", "channel": channel_id})
            return
        if behavior.playlist_format != "hls":
            self.write_json(HTTPStatus.NOT_FOUND, {"error": "hls_not_enabled", "channel": channel_id})
            return

        lines: list[str] = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-TARGETDURATION:1",
            "#EXT-X-MEDIA-SEQUENCE:0",
        ]
        for segment_index in range(behavior.hls_segment_count):
            lines.append("#EXTINF:1.000,")
            lines.append(f"segments/{segment_index}.ts")
        if behavior.hls_endlist:
            lines.append("#EXT-X-ENDLIST")

        payload = "\n".join(lines).encode("utf-8")
        state.log_event("hls_manifest_served", channel=channel_id, segments=behavior.hls_segment_count)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def handle_hls_segment(self, channel_id: str, segment_id: str) -> None:
        state = self.server.state
        behavior = state.behavior_for_channel(channel_id)
        if behavior is None:
            state.log_event("hls_segment_unknown_channel", channel=channel_id, segment=segment_id)
            self.write_json(HTTPStatus.NOT_FOUND, {"error": "unknown_channel", "channel": channel_id})
            return
        if behavior.playlist_format != "hls":
            self.write_json(HTTPStatus.NOT_FOUND, {"error": "hls_not_enabled", "channel": channel_id})
            return

        try:
            segment_index = int(segment_id)
        except ValueError:
            self.write_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_segment", "segment": segment_id})
            return
        if segment_index < 0 or segment_index >= behavior.hls_segment_count:
            self.write_json(HTTPStatus.NOT_FOUND, {"error": "unknown_segment", "segment": segment_id})
            return

        chunks = [
            self.payload_chunk_for_behavior(behavior, segment_index * behavior.hls_segment_chunks + chunk_index)
            for chunk_index in range(behavior.hls_segment_chunks)
        ]
        payload = b"".join(chunks)
        state.log_event("hls_segment_served", channel=channel_id, segment=segment_index, bytes=len(payload))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", behavior.content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def handle_channel_stream(self, channel_id: str, query: dict[str, list[str]]) -> None:
        state = self.server.state
        behavior = state.behavior_for_channel(channel_id)
        if behavior is None:
            state.log_event("stream_unknown_channel", channel=channel_id)
            self.write_json(HTTPStatus.NOT_FOUND, {"error": "unknown_channel", "channel": channel_id})
            return

        if not self._provider_auth_ok(query):
            state.log_event("stream_unauthorized", channel=channel_id)
            self.write_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid_credentials", "channel": channel_id})
            return

        accepted, counts, reason = state.try_open_stream(channel_id)
        if not accepted:
            state.log_event("stream_rejected", channel=channel_id, reason=reason, **counts)
            status = HTTPStatus.SERVICE_UNAVAILABLE if reason == "provider_unhealthy" else HTTPStatus.TOO_MANY_REQUESTS
            self.write_json(status, {"error": reason, "channel": channel_id, **counts})
            return

        request_mode = query.get("failure_mode", [behavior.failure_mode])[0]
        broken = False
        start_time = time.monotonic()
        connection_id = f"{channel_id}-{int(start_time * 1000)}-{threading.get_ident()}"
        client_address = self.client_address[0] if self.client_address else ""
        state.log_event(
            "stream_opened",
            channel=channel_id, connection_id=connection_id, failure_mode=request_mode,
            client_address=client_address, **counts,
        )

        try:
            if request_mode in {"immediate_error", "http_error"}:
                broken = True
                status_code = HTTPStatus.SERVICE_UNAVAILABLE if request_mode == "immediate_error" else behavior.failure_status_code
                state.log_event(
                    "stream_http_error",
                    channel=channel_id,
                    connection_id=connection_id,
                    status_code=int(status_code),
                    retry_after_seconds=behavior.retry_after_seconds,
                )
                self.write_error_response(
                    status_code,
                    {
                        "error": "injected_http_error",
                        "channel": channel_id,
                        "status_code": int(status_code),
                    },
                    retry_after_seconds=behavior.retry_after_seconds)
                return

            if request_mode in {"disconnect_before_headers", "reset_before_headers"}:
                broken = True
                state.log_event(
                    "stream_disconnected_before_headers",
                    channel=channel_id,
                    connection_id=connection_id,
                    mode=request_mode,
                )
                self.close_connection = True
                if request_mode == "reset_before_headers":
                    # SO_LINGER with a zero timeout makes close() send a TCP
                    # RST instead of a FIN. Deliberately no shutdown() first:
                    # shutdown(SHUT_WR) would emit a FIN and the client would
                    # observe a graceful EOF, making this mode
                    # indistinguishable from disconnect_before_headers -- the
                    # exact bug this branch used to have.
                    try:
                        self.connection.setsockopt(
                            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                        )
                    except OSError:
                        pass
                else:
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                self.connection.close()
                return

            if behavior.startup_delay_ms > 0:
                time.sleep(behavior.startup_delay_ms / 1000.0)

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", behavior.content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()

            for chunk_index in range(behavior.chunk_count):
                elapsed = time.monotonic() - start_time
                if request_mode == "close_after_seconds" and behavior.failure_after_seconds > 0 and elapsed >= behavior.failure_after_seconds:
                    broken = True
                    state.log_event("stream_broken", channel=channel_id, connection_id=connection_id, mode=request_mode, after_seconds=behavior.failure_after_seconds)
                    return

                if request_mode in {"close_after_chunks", "stall_after_chunks"} and behavior.failure_after_chunks > 0 and chunk_index >= behavior.failure_after_chunks:
                    broken = True
                    state.log_event("stream_broken", channel=channel_id, connection_id=connection_id, mode=request_mode, after_chunks=behavior.failure_after_chunks)
                    if request_mode == "stall_after_chunks":
                        # Bracketing events so the emitted stall duration is
                        # provable from the log alone (requirement-10 style
                        # stall_started/stall_ended), not inferred from a gap
                        # between stream_broken and stream_closed. Today the
                        # connection always closes after the stall;
                        # stall-then-resume on the same connection is a
                        # planned capability, not yet expressible.
                        state.log_event("stall_started", channel=channel_id, connection_id=connection_id, stall_seconds=behavior.stall_seconds)
                        time.sleep(behavior.stall_seconds)
                        state.log_event("stall_ended", channel=channel_id, connection_id=connection_id, stall_seconds=behavior.stall_seconds)
                    return

                # switch_source can mutate payload fields for an already-open
                # connection mid-stream (Stage A7 baseline #13); everything
                # else about this connection (failure_mode/chunk_count/timing
                # above) intentionally still comes from `behavior`, captured
                # once at connection-open -- see ALLOWED_LIVE_SOURCE_FIELDS.
                live_source = state.current_live_source(channel_id)
                payload_behavior = replace(behavior, **live_source) if live_source else behavior
                chunk = self.payload_chunk_for_behavior(payload_behavior, chunk_index)
                self.wfile.write(chunk)
                self.wfile.flush()
                wrapped = False
                if payload_behavior.payload_mode == "file" and chunk_index > 0:
                    # Stage A8.5 result-artifact requirement: record whether
                    # this connection's file-loop payload wrapped back to
                    # byte zero mid-stream (the intentional baseline #3
                    # behavior, and a real thing to flag for any other file
                    # fixture sized shorter than chunk_count * chunk_bytes).
                    payload_len = len(_read_payload_file(payload_behavior.payload_file))
                    if payload_len:
                        chunk_bytes = payload_behavior.payload_chunk_bytes
                        wrapped = (chunk_index * chunk_bytes) // payload_len > ((chunk_index - 1) * chunk_bytes) // payload_len
                state.record_chunk_sent(
                    channel_id, len(chunk),
                    connection_id=connection_id, chunk_index=chunk_index, wrapped=wrapped,
                )
                if behavior.chunk_interval_ms > 0:
                    time.sleep(behavior.chunk_interval_ms / 1000.0)
        except BrokenPipeError:
            state.log_event("client_disconnected", channel=channel_id, connection_id=connection_id)
        except ConnectionResetError:
            state.log_event("client_reset_connection", channel=channel_id, connection_id=connection_id)
        finally:
            counts = state.close_stream(channel_id, broken=broken)
            state.log_event(
                "stream_closed",
                channel=channel_id, connection_id=connection_id, broken=broken,
                client_address=client_address, **counts,
            )

    def payload_chunk_for_behavior(self, behavior: ChannelBehavior, chunk_index: int) -> bytes:
        if behavior.payload_mode == "ts":
            if behavior.ts_variant in TS_DYNAMIC_FAULT_VARIANTS:
                packet, fault_event = _ts_dynamic_packet(behavior.ts_variant, chunk_index)
                if fault_event:
                    self.server.state.log_event(fault_event, channel=behavior.channel_id, chunk_index=chunk_index)
                return packet
            sequence = _TS_SEQUENCE_NO_IDR if behavior.ts_variant == "no_idr" else _TS_SEQUENCE
            return sequence[chunk_index % len(sequence)]
        if behavior.payload_mode == "file":
            file_payload = _read_payload_file(behavior.payload_file)
            if not file_payload:
                raise RuntimeError(f"payload file is empty: {behavior.payload_file}")
            return _file_payload_chunk(file_payload, chunk_index, behavior.payload_chunk_bytes)
        return (
            f"{behavior.payload}|provider={self.server.state.provider_name}|"
            f"channel={behavior.channel_id}|chunk={chunk_index}\n"
        ).encode("utf-8")

    # ------------------------------------------------------------------
    # EPG / XMLTV handler
    # ------------------------------------------------------------------

    def handle_epg(self) -> None:
        state = self.server.state
        epg = state.epg
        if epg is None:
            self.write_json(HTTPStatus.NOT_FOUND, {"error": "epg_not_configured"})
            return

        lines: list[str] = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<!DOCTYPE tv SYSTEM "xmltv.dtd">',
            '<tv generator-info-name="provider-sim">',
        ]

        for ch in epg.channels:
            ch_id = ch.get("id", "")
            ch_name = _xml_escape(ch.get("name", ch_id))
            icon = ch.get("icon", "")
            lines.append(f'  <channel id="{_xml_attr(ch_id)}">')
            lines.append(f'    <display-name>{ch_name}</display-name>')
            if icon:
                lines.append(f'    <icon src="{_xml_attr(icon)}" />')
            lines.append("  </channel>")

        for prog in epg.programmes:
            ch_id = prog.get("channel", "")
            start = prog.get("start", "20260101000000 +0000")
            stop = prog.get("stop", "20260101010000 +0000")
            title = _xml_escape(prog.get("title", ""))
            desc = _xml_escape(prog.get("desc", ""))
            lines.append(
                f'  <programme start="{_xml_attr(start)}" stop="{_xml_attr(stop)}" channel="{_xml_attr(ch_id)}">'
            )
            lines.append(f"    <title>{title}</title>")
            if desc:
                lines.append(f"    <desc>{desc}</desc>")
            lines.append("  </programme>")

        lines.append("</tv>")
        payload = "\n".join(lines).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    # ------------------------------------------------------------------
    # Xtream Codes handlers
    # ------------------------------------------------------------------

    def _xtream_auth_ok(self, query: dict[str, list[str]]) -> bool:
        """Check username/password query params against fixture Xtream config."""
        xtream = self.server.state.xtream
        if xtream is None:
            return False
        username = (query.get("username") or [""])[0]
        password = (query.get("password") or [""])[0]
        return username == xtream.username and password == xtream.password

    def handle_xtream_api(self, query: dict[str, list[str]]) -> None:
        state = self.server.state
        xtream = state.xtream
        if xtream is None:
            self.write_json(HTTPStatus.NOT_FOUND, {"error": "xtream_not_configured"})
            return

        if not self._xtream_auth_ok(query):
            self.write_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid_credentials"})
            return

        action = (query.get("action") or [""])[0]

        if action == "get_account_info":
            self.write_json(HTTPStatus.OK, {
                "user_info": {
                    "username": xtream.username,
                    "password": "***",
                    "status": "Active",
                    "exp_date": "9999999999",
                    "is_trial": "0",
                    "active_cons": "0",
                    "created_at": "0",
                    "max_connections": "10",
                    "allowed_output_formats": ["ts", "m3u8", "rtmp"],
                },
                "server_info": {
                    "url": self.server.public_base_url,
                    "port": "",
                    "https_port": "",
                    "server_protocol": "http",
                    "rtmp_port": "",
                    "timezone": "UTC",
                    "timestamp_now": int(time.time()),
                    "time_now": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                },
            })
            return

        if action == "get_live_categories":
            self.write_json(HTTPStatus.OK, xtream.categories or [])
            return

        if action == "get_live_streams":
            streams = self._build_xtream_stream_list(xtream)
            self.write_json(HTTPStatus.OK, streams)
            return

        if action == "get_vod_categories":
            self.write_json(HTTPStatus.OK, xtream.vod_categories)
            return
        if action == "get_vod_streams":
            self.write_json(HTTPStatus.OK, xtream.vod_streams)
            return
        if action == "get_series_categories":
            self.write_json(HTTPStatus.OK, xtream.series_categories)
            return
        if action == "get_series":
            self.write_json(HTTPStatus.OK, xtream.series)
            return

        if action == "get_series_info":
            series_id = (query.get("series_id") or [""])[0]
            self.write_json(
                HTTPStatus.OK,
                xtream.series_info.get(series_id, {"info": {}, "episodes": {}, "seasons": []}),
            )
            return

        self.write_json(HTTPStatus.BAD_REQUEST, {"error": f"unknown_action: {action}"})

    def handle_xtream_get_m3u(self, query: dict[str, list[str]]) -> None:
        """GET /get.php?username=X&password=Y — return M3U with Xtream-style entries."""
        state = self.server.state
        xtream = state.xtream
        if xtream is None:
            self.write_json(HTTPStatus.NOT_FOUND, {"error": "xtream_not_configured"})
            return
        if not self._xtream_auth_ok(query):
            self.write_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid_credentials"})
            return

        base = self.server.public_base_url
        username = xtream.username
        password = xtream.password
        lines: list[str] = ["#EXTM3U"]
        for channel_id, behavior in state.channels.items():
            stream_id = _xtream_stream_id(xtream, channel_id)
            display_name = behavior.display_name or channel_id
            group_title = behavior.group_title or state.provider_name
            tvg_id = behavior.tvg_id or channel_id
            stream_url = f"{base}/live/{username}/{password}/{stream_id}.ts"
            lines.append(
                f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{display_name}" group-title="{group_title}",{display_name}'
            )
            lines.append(stream_url)

        payload = "\n".join(lines).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-mpegurl")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def handle_xtream_stream(self, path: str, query: dict[str, list[str]]) -> None:
        """
        Handle GET /live/{user}/{pass}/{streamId}[.ts]

        Resolves the stream_id back to a channel_id and delegates to the
        standard channel-stream handler.
        """
        state = self.server.state
        xtream = state.xtream

        # Parse /live/{user}/{pass}/{streamIdMaybeWithExt}
        parts = path.lstrip("/").split("/")
        if len(parts) < 4:
            self.write_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_path"})
            return

        req_user = parts[1]
        req_pass = parts[2]
        stream_id_str = parts[3].removesuffix(".ts")

        if xtream is None or req_user != xtream.username or req_pass != xtream.password:
            self.write_json(HTTPStatus.FORBIDDEN, {"error": "invalid_credentials"})
            return

        try:
            stream_id = int(stream_id_str)
        except ValueError:
            self.write_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_stream_id"})
            return

        # Reverse-lookup channel_id from stream_id
        channel_id = _xtream_channel_for_stream_id(xtream, state.channels, stream_id)
        if channel_id is None:
            self.write_json(HTTPStatus.NOT_FOUND, {"error": "unknown_stream_id"})
            return

        self.handle_channel_stream(channel_id, query)

    def handle_xtream_on_demand(self, path: str) -> None:
        state = self.server.state
        xtream = state.xtream
        parts = path.lstrip("/").split("/")
        if len(parts) != 4 or xtream is None:
            self.write_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_path"})
            return
        kind, username, password, id_with_extension = parts
        if username != xtream.username or password != xtream.password:
            self.write_json(HTTPStatus.FORBIDDEN, {"error": "invalid_credentials"})
            return
        content_id = id_with_extension.split(".", 1)[0]
        known_ids = {
            str(item.get("stream_id")) for item in xtream.vod_streams
        }
        if kind == "series":
            known_ids = {
                str(episode.get("id"))
                for info in xtream.series_info.values()
                for episodes in info.get("episodes", {}).values()
                for episode in episodes
            }
        if content_id not in known_ids:
            self.write_json(HTTPStatus.NOT_FOUND, {"error": "unknown_content"})
            return

        payload = playback_media(f"{kind}:{content_id}")
        etag = f'"lab-{kind}-{content_id}-v1"'
        range_header = self.headers.get("Range")
        if_range = self.headers.get("If-Range")
        selected = None
        if range_header and (not if_range or if_range == etag):
            try:
                selected = parse_single_range(range_header, len(payload))
            except (ValueError, TypeError):
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{len(payload)}")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return

        body = payload if selected is None else payload[selected.start:selected.end + 1]
        status = HTTPStatus.OK if selected is None else HTTPStatus.PARTIAL_CONTENT
        state.log_event(
            "ondemand_served",
            kind=kind,
            content_id=content_id,
            range=range_header or "",
            status=int(status),
            bytes=len(body),
        )
        self.send_response(status)
        self.send_header("Content-Type", "video/x-matroska")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        if selected is not None:
            self.send_header("Content-Range", selected.content_range)
        self.send_header("Connection", "close")
        self.end_headers()
        chunk_interval = 0.12 if content_id in {"30002", "50002"} else 0.005
        try:
            for offset in range(0, len(body), 4096):
                self.wfile.write(body[offset:offset + 4096])
                self.wfile.flush()
                time.sleep(chunk_interval)
        except (BrokenPipeError, ConnectionResetError):
            state.log_event(
                "ondemand_disconnected",
                kind=kind,
                content_id=content_id,
                range=range_header or "",
            )

    def _build_xtream_stream_list(self, xtream: XtreamConfig) -> list[dict[str, Any]]:
        state = self.server.state
        streams = []
        for channel_id, behavior in state.channels.items():
            stream_id = _xtream_stream_id(xtream, channel_id)
            display_name = behavior.display_name or channel_id
            group_title = behavior.group_title or state.provider_name
            tvg_id = behavior.tvg_id or channel_id
            # Match category_id from categories by name
            cat_id = "1"
            for cat in xtream.categories:
                if cat.get("category_name") == group_title:
                    cat_id = str(cat.get("category_id", "1"))
                    break
            streams.append({
                "stream_id": stream_id,
                "name": display_name,
                "stream_type": "live",
                "stream_icon": "",
                "epg_channel_id": tvg_id,
                "added": "0",
                "category_id": cat_id,
                "tv_archive": 0,
                "direct_source": "",
                "tv_archive_duration": 0,
            })
        return streams

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def write_json(self, status: HTTPStatus, body: dict[str, Any] | list) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def write_error_response(
        self,
        status: int | HTTPStatus,
        body: dict[str, Any],
        *,
        retry_after_seconds: int = 0,
    ) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        if retry_after_seconds > 0:
            self.send_header("Retry-After", str(retry_after_seconds))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def read_json_body(self) -> dict[str, Any]:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return {}
        if content_length <= 0:
            return {}
        read_size = min(content_length, MAX_JSON_BODY_BYTES)
        raw = self.rfile.read(read_size)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def log_message(self, format: str, *args: Any) -> None:
        return


# ---------------------------------------------------------------------------
# Xtream stream-ID helpers
# ---------------------------------------------------------------------------

def _xtream_stream_id(xtream: XtreamConfig, channel_id: str) -> int:
    """
    Return the stable 31-bit stream_id for a channel_id.
    Uses the fixture-configured mapping if present, otherwise derives from MD5.
    """
    if channel_id in xtream.stream_ids:
        return xtream.stream_ids[channel_id]
    digest = hashlib.md5(channel_id.encode()).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def _xtream_channel_for_stream_id(
    xtream: XtreamConfig,
    channels: dict[str, ChannelBehavior],
    stream_id: int,
) -> str | None:
    """Reverse-map a stream_id back to a channel_id."""
    for channel_id in channels:
        if _xtream_stream_id(xtream, channel_id) == stream_id:
            return channel_id
    return None


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

def _xml_escape(text: str) -> str:
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _xml_attr(text: str) -> str:
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------

def build_behavior(channel_id: str, channel_config: dict[str, Any], defaults: dict[str, Any]) -> ChannelBehavior:
    merged = {**defaults, **channel_config}
    failure_mode = str(merged.get("failure_mode", DEFAULT_FAILURE_MODE))
    if failure_mode not in VALID_FAILURE_MODES:
        raise SystemExit(
            f"Channel '{channel_id}' has unknown failure_mode={failure_mode!r}. "
            f"Valid modes: {', '.join(sorted(VALID_FAILURE_MODES))}."
        )
    payload_mode = str(merged.get("payload_mode", DEFAULT_PAYLOAD_MODE))
    if payload_mode not in VALID_PAYLOAD_MODES:
        raise SystemExit(
            f"Channel '{channel_id}' has unknown payload_mode={payload_mode!r}. "
            f"Valid modes: {', '.join(sorted(VALID_PAYLOAD_MODES))}."
        )
    ts_variant = str(merged.get("ts_variant", DEFAULT_TS_VARIANT))
    if ts_variant not in VALID_TS_VARIANTS:
        raise SystemExit(
            f"Channel '{channel_id}' has unknown ts_variant={ts_variant!r}. "
            f"Valid variants: {', '.join(sorted(VALID_TS_VARIANTS))}."
        )
    payload_file = str(merged.get("payload_file", ""))
    generate = merged.get("generate")
    if generate is not None:
        if payload_file:
            raise SystemExit(
                f"Channel '{channel_id}' has both 'generate' and 'payload_file' -- "
                f"payload_file is computed automatically from generate: and must not "
                f"also be set explicitly."
            )
        # generate: only names WHERE the materialized file will be (a pure,
        # deterministic hash of worker+base+args) -- it never invokes Docker
        # itself. Producing the file is run-scenario.sh's job, via
        # materialize_fixtures.py, before the server starts; if it hasn't run
        # yet, _resolve_payload_file() below fails closed with a clear
        # missing-file error rather than silently starting half-configured.
        from materialize_fixtures import MaterializationError, target_path as _generated_target_path

        try:
            payload_file = str(
                _generated_target_path(generate["worker"], generate["base"], generate["args"])
            )
        except MaterializationError as exc:
            raise SystemExit(f"Channel '{channel_id}' generate: {exc}") from exc
    payload_chunk_bytes = int(merged.get("payload_chunk_bytes", DEFAULT_FILE_CHUNK_BYTES))
    playlist_format = str(merged.get("playlist_format", DEFAULT_PLAYLIST_FORMAT))
    if playlist_format not in VALID_PLAYLIST_FORMATS:
        raise SystemExit(
            f"Channel '{channel_id}' has unknown playlist_format={playlist_format!r}. "
            f"Valid formats: {', '.join(sorted(VALID_PLAYLIST_FORMATS))}."
        )
    hls_segment_count = int(merged.get("hls_segment_count", DEFAULT_HLS_SEGMENT_COUNT))
    if hls_segment_count <= 0:
        raise SystemExit(f"Channel '{channel_id}' hls_segment_count must be greater than zero.")
    hls_segment_chunks = int(merged.get("hls_segment_chunks", DEFAULT_HLS_SEGMENT_CHUNKS))
    if hls_segment_chunks <= 0:
        raise SystemExit(f"Channel '{channel_id}' hls_segment_chunks must be greater than zero.")
    hls_endlist = bool(merged.get("hls_endlist", DEFAULT_HLS_ENDLIST))
    if payload_chunk_bytes <= 0:
        raise SystemExit(f"Channel '{channel_id}' payload_chunk_bytes must be greater than zero.")
    if payload_mode == "file":
        if not payload_file:
            raise SystemExit(f"Channel '{channel_id}' payload_mode='file' requires payload_file.")
        payload_file = str(_resolve_payload_file(payload_file))
    return ChannelBehavior(
        channel_id=channel_id,
        content_type=str(merged.get("content_type", DEFAULT_CONTENT_TYPE)),
        chunk_count=int(merged.get("chunk_count", DEFAULT_CHUNK_COUNT)),
        chunk_interval_ms=int(merged.get("chunk_interval_ms", DEFAULT_CHUNK_INTERVAL_MS)),
        startup_delay_ms=int(merged.get("startup_delay_ms", DEFAULT_STARTUP_DELAY_MS)),
        failure_mode=failure_mode,
        failure_after_chunks=int(merged.get("failure_after_chunks", DEFAULT_FAILURE_AFTER_CHUNKS)),
        failure_after_seconds=float(merged.get("failure_after_seconds", DEFAULT_FAILURE_AFTER_SECONDS)),
        stall_seconds=float(merged.get("stall_seconds", DEFAULT_STALL_SECONDS)),
        failure_status_code=int(merged.get("failure_status_code", DEFAULT_FAILURE_STATUS_CODE)),
        retry_after_seconds=int(merged.get("retry_after_seconds", DEFAULT_RETRY_AFTER_SECONDS)),
        payload=str(merged.get("payload", f"stream-{channel_id}")),
        payload_mode=payload_mode,
        ts_variant=ts_variant,
        payload_file=payload_file,
        payload_chunk_bytes=payload_chunk_bytes,
        playlist_format=playlist_format,
        hls_segment_count=hls_segment_count,
        hls_segment_chunks=hls_segment_chunks,
        hls_endlist=hls_endlist,
        display_name=str(channel_config.get("display_name", "")),
        group_title=str(channel_config.get("group_title", "")),
        tvg_id=str(channel_config.get("tvg_id", "")),
    )


def _load_epg_config(raw: dict[str, Any]) -> EpgConfig | None:
    epg_raw = raw.get("epg")
    if not epg_raw:
        return None
    return EpgConfig(
        channels=list(epg_raw.get("channels", [])),
        programmes=list(epg_raw.get("programmes", [])),
        path=str(epg_raw.get("path", "/epg/guide.xml")),
    )


def _load_xtream_config(raw: dict[str, Any]) -> XtreamConfig | None:
    xtream_raw = raw.get("xtream")
    if not xtream_raw:
        return None
    return XtreamConfig(
        username=str(xtream_raw.get("username", "testuser")),
        password=str(xtream_raw.get("password", "testpass")),
        categories=list(xtream_raw.get("categories", [])),
        stream_ids=dict(xtream_raw.get("stream_ids", {})),
        vod_categories=list(xtream_raw.get("vod_categories", [])),
        vod_streams=list(xtream_raw.get("vod_streams", [])),
        series_categories=list(xtream_raw.get("series_categories", [])),
        series=list(xtream_raw.get("series", [])),
        series_info=dict(xtream_raw.get("series_info", {})),
    )


def _load_auth_config(raw: dict[str, Any]) -> AuthConfig | None:
    auth_raw = raw.get("authentication")
    if not auth_raw:
        return None
    mode = str(auth_raw.get("mode", "static"))
    if mode not in VALID_AUTH_MODES:
        raise SystemExit(
            f"unknown provider.authentication.mode={mode!r} (known: {sorted(VALID_AUTH_MODES)})"
        )
    return AuthConfig(
        mode=mode,
        username=str(auth_raw.get("username", "test")),
        password=str(auth_raw.get("password", "test")),
    )


class _DuplicateKeyCheckingLoader(yaml.SafeLoader):
    """
    Plain yaml.safe_load silently keeps the last of any duplicate mapping
    key -- e.g. two `channel-a:` entries under `channels:` collapse into one
    with no warning. That's exactly the kind of "started half-configured"
    MockServer's failOnInitializationError lesson (Stage A5's fail-closed
    atomic loading decision) exists to catch, so this loader raises instead.
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"found duplicate key {key!r}", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise ValueError(f"found duplicate key {key!r}")
        seen.add(key)
    return dict(pairs)


def _parse_config_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    # yaml.safe_load parses plain JSON fine too (JSON is a YAML subset), but
    # dispatching on extension keeps error messages format-specific and
    # avoids surprises for either file type. Both branches reject duplicate
    # mapping keys (e.g. two `channel-a:` entries) rather than silently
    # keeping the last one -- see _DuplicateKeyCheckingLoader.
    try:
        if path.suffix.lower() in (".yaml", ".yml"):
            data = yaml.load(text, Loader=_DuplicateKeyCheckingLoader)
        else:
            data = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except (yaml.YAMLError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"{path} failed to parse: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"{path} did not parse to a mapping")
    return data


def _normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize either input shape into one flat internal shape. A legacy
    fixture (provider_name/max_streams/channels at the top level) and a
    scenario file (the same fields nested under `provider:`, plus an
    optional `sequence:`) both produce the same keys here -- a fixture is
    exactly a scenario with an empty `sequence`, per the Stage A5 decision.
    """
    flat = dict(config)
    provider = flat.pop("provider", None)
    if isinstance(provider, dict):
        flat.setdefault("provider_name", provider.get("name"))
        flat.setdefault("max_streams", provider.get("max_streams", DEFAULT_MAX_STREAMS))
        flat.setdefault("defaults", provider.get("defaults", {}))
        flat.setdefault("channels", provider.get("channels", {}))
        flat.setdefault("authentication", provider.get("authentication", {}))
    flat.setdefault("sequence", flat.get("sequence", []))
    flat.setdefault("expected_simulator_events", flat.get("expected_simulator_events", []))
    flat.setdefault("schema_version", flat.get("schema_version"))
    return flat


SCENARIO_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "provider-scenario.schema.json"


def _load_scenario_schema() -> dict[str, Any]:
    return json.loads(SCENARIO_SCHEMA_PATH.read_text(encoding="utf-8"))


def _parse_dotted_version(text: Any) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(text).split("."))
    except ValueError:
        raise SystemExit(f"invalid version string: {text!r} (expected dotted integers, e.g. '0.2.0')")


def validate_scenario_config(raw_config: dict[str, Any], config: dict[str, Any], path: Path) -> None:
    """
    Stage A5's fail-closed atomic loading decision: a scenario with an
    unknown sequence action/trigger key, an override field/value the engine
    doesn't understand, an unsupported schema_version, or a
    minimum_engine_version newer than this build must refuse to start --
    raise here, before load_scenario()'s caller ever binds a port, rather
    than discover the problem mid-run. (Missing referenced media files are
    already fail-closed via _resolve_payload_file(), called eagerly from
    build_behavior() during channel construction below; duplicate channel
    IDs are already fail-closed during parsing, by
    _DuplicateKeyCheckingLoader / _reject_duplicate_json_keys.)

    `raw_config` (pre-normalization) is validated structurally against the
    published JSON Schema when it uses the nested `provider:` scenario
    shape; legacy flat fixtures (no `provider:` key) skip that structural
    pass -- see the schema file's own docstring for why -- but still get
    every semantic check below via `config` (post-normalization).
    """
    errors: list[str] = []

    if "provider" in raw_config:
        validator = jsonschema.Draft202012Validator(_load_scenario_schema())
        errors.extend(
            f"{'.'.join(str(p) for p in error.absolute_path) or '<root>'}: {error.message}"
            for error in validator.iter_errors(raw_config)
        )

    schema_version = config.get("schema_version")
    if schema_version is not None and schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        errors.append(
            f"unsupported schema_version={schema_version!r} "
            f"(this engine understands: {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
        )

    minimum_engine_version = config.get("minimum_engine_version")
    if minimum_engine_version is not None:
        required = _parse_dotted_version(minimum_engine_version)
        if required > ENGINE_VERSION:
            errors.append(
                f"minimum_engine_version={minimum_engine_version!r} is newer than "
                f"this engine ({'.'.join(str(p) for p in ENGINE_VERSION)})"
            )

    channel_ids = set(dict(config.get("channels", {})).keys())
    for index, step in enumerate(config.get("sequence") or []):
        label = f"sequence[{index}]"
        if not isinstance(step, dict):
            errors.append(f"{label} is not a mapping")
            continue
        action = step.get("action")
        if action not in VALID_SEQUENCE_ACTIONS:
            errors.append(f"{label} has unknown action={action!r} (known: {sorted(VALID_SEQUENCE_ACTIONS)})")
            continue
        trigger = step.get("trigger")
        if trigger is not None:
            after = trigger.get("after") if isinstance(trigger, dict) else None
            if not isinstance(after, dict):
                errors.append(f"{label} has a malformed trigger (expected trigger.after.{{seconds|connections|chunks|bytes}})")
            else:
                unknown_after = sorted(set(after) - VALID_TRIGGER_AFTER_KEYS)
                if unknown_after:
                    errors.append(f"{label} trigger.after has unknown key(s): {', '.join(unknown_after)}")
        if action in ("set_channel_behavior", "switch_source"):
            channel = step.get("channel")
            if channel not in channel_ids:
                errors.append(f"{label} references unknown channel={channel!r}")
            override_body = {
                key: value for key, value in step.items() if key not in ("action", "channel", "trigger", "id")
            }
            validator = _validate_override_body if action == "set_channel_behavior" else _validate_live_source_body
            _, override_error = validator(override_body)
            if override_error is not None:
                errors.append(f"{label}: {override_error}")

    if errors:
        raise SystemExit(f"{path} failed fail-closed validation:\n  - " + "\n  - ".join(errors))


def load_scenario(
    path: Path,
    override_provider_name: str | None = None,
    override_max_streams: int | None = None,
    override_run_id: str | None = None,
) -> ProviderState:
    """
    Load either a legacy fixture JSON file or a scenario YAML/JSON file.
    Both `--fixture` and `--scenario` route here -- see the CLI/loader
    unification decision in docs/M3Undle_Provider_Simulator_Implementation_Plan.md
    Stage A5.
    """
    raw_config = _parse_config_file(path)
    config = _normalize_config(raw_config)
    validate_scenario_config(raw_config, config, path)
    provider_name = override_provider_name or str(config.get("provider_name") or path.stem)
    defaults = dict(config.get("defaults", {}))
    max_streams = override_max_streams if override_max_streams is not None else int(config.get("max_streams", DEFAULT_MAX_STREAMS))
    channels = {
        channel_id: build_behavior(channel_id, dict(channel_config), defaults)
        for channel_id, channel_config in dict(config.get("channels", {})).items()
    }
    if not channels:
        raise SystemExit(f"{path} does not define any channels.")
    epg = _load_epg_config(config)
    xtream = _load_xtream_config(config)
    auth = _load_auth_config(config)
    sequence = list(config.get("sequence", []))
    return ProviderState(
        provider_name=provider_name,
        max_streams=max_streams,
        channels=channels,
        epg=epg,
        xtream=xtream,
        auth=auth,
        fixture_path=path,
        sequence=sequence,
        scenario_id=config.get("name"),
        run_id=override_run_id,
        expected_simulator_events=list(config.get("expected_simulator_events") or []),
        run_config=dict(config.get("run") or {}),
    )


class ScenarioSequenceRunner(threading.Thread):
    """
    Drives a scenario's `sequence:` natively, in-process -- the Stage A5
    architecture target. Calls the same ProviderState methods
    /debug/channels/{id}/override already exposes over HTTP, just without
    the HTTP round trip, and tags the resulting events with an action_id
    so callers can correlate "which step produced this event."

    Deliberately small: only `after: {seconds|connections|chunks|bytes}`
    triggers and the `set_channel_behavior`/`wait`/`switch_source` actions
    are implemented. validate_scenario_config() rejects anything outside
    that vocabulary at load time (fail-closed, before the server ever binds
    a port), so the "log a diagnostic event rather than raising" fallback below should be
    unreachable for any scenario that passed loading -- it stays as
    defense-in-depth, not the primary gate, so a future validator bug can't
    take down an already-running simulator either.
    """

    # How long a metric trigger (connections/chunks/bytes) may wait before
    # the sequence aborts. An instance attribute so tests can shorten it;
    # not yet scenario-configurable (see the plan's Requirement Coverage
    # Ledger).
    trigger_timeout_seconds = 60.0

    def __init__(self, state: ProviderState, sequence: list[dict[str, Any]]) -> None:
        super().__init__(daemon=True, name="scenario-sequence")
        self.state = state
        self.sequence = sequence

    def run(self) -> None:
        for index, step in enumerate(self.sequence):
            action_id = str(step.get("id", f"step-{index}"))
            if not self._resolve_trigger(step, action_id):
                # The trigger timed out: the stream never reached the state
                # this step was waiting for. Applying the action anyway (the
                # old behavior) would fire the fault at a meaningless moment
                # and every later step would build on a state the scenario
                # never actually reached -- for a determinism-first engine
                # the honest outcome is to stop and say so.
                self.state.log_event("scenario_sequence_aborted", action_id=action_id)
                return
            self._apply_action(step, action_id)

    def _resolve_trigger(self, step: dict[str, Any], action_id: str) -> bool:
        """Returns True when the step may proceed (trigger satisfied, no
        trigger, or unknown-trigger fallback), False when it timed out."""
        trigger = step.get("trigger")
        if not trigger:
            return True
        if not isinstance(trigger, dict):
            self.state.log_event("scenario_trigger_unsupported", trigger=trigger, action_id=action_id)
            return True
        after = trigger.get("after", {})
        if not isinstance(after, dict):
            self.state.log_event("scenario_trigger_unsupported", trigger=trigger, action_id=action_id)
            return True
        if "seconds" in after:
            time.sleep(float(after["seconds"]))
            return True
        channel = step.get("channel")
        if "connections" in after:
            return self._wait_for_channel_metric(
                target=int(after["connections"]),
                trigger=trigger,
                action_id=action_id,
                counter=lambda: self.state.opened_by_channel.get(channel, 0),
            )
        if "chunks" in after:
            return self._wait_for_channel_metric(
                target=int(after["chunks"]),
                trigger=trigger,
                action_id=action_id,
                counter=lambda: self.state.chunks_sent_by_channel.get(channel, 0),
            )
        if "bytes" in after:
            return self._wait_for_channel_metric(
                target=int(after["bytes"]),
                trigger=trigger,
                action_id=action_id,
                counter=lambda: self.state.bytes_sent_by_channel.get(channel, 0),
            )
        self.state.log_event("scenario_trigger_unsupported", trigger=trigger, action_id=action_id)
        return True

    def _wait_for_channel_metric(
        self,
        *,
        target: int,
        trigger: dict[str, Any],
        action_id: str,
        counter: Callable[[], int],
    ) -> bool:
        """Poll a per-channel counter (accepted opens, chunks, or bytes sent)
        until it reaches `target` (True), or log a timeout event after 60s and
        return False so run() aborts the remaining sequence."""
        deadline = time.monotonic() + self.trigger_timeout_seconds
        while time.monotonic() < deadline:
            with self.state.lock:
                seen = counter()
            if seen >= target:
                return True
            time.sleep(0.1)
        self.state.log_event("scenario_trigger_timeout", trigger=trigger, action_id=action_id)
        return False

    def _apply_action(self, step: dict[str, Any], action_id: str) -> None:
        action = step.get("action")
        if action == "set_channel_behavior":
            channel = step["channel"]
            override = {
                key: value
                for key, value in step.items()
                if key not in ("action", "channel", "trigger", "id")
            }
            self.state.set_channel_override(channel, override, action_id=action_id)
            return
        if action == "switch_source":
            channel = step["channel"]
            override = {
                key: value
                for key, value in step.items()
                if key not in ("action", "channel", "trigger", "id")
            }
            self.state.set_live_source(channel, override, action_id=action_id)
            return
        if action == "wait":
            time.sleep(float(step.get("duration_s", 0)))
            return
        self.state.log_event("scenario_action_unsupported", action=action, action_id=action_id)


# ---------------------------------------------------------------------------
# Stage A8.5 -- Scenario run/verify (self-verification) mode
# ---------------------------------------------------------------------------
# Requirement 11 / the requirements doc's own north-star had no machinery:
# expected_simulator_events was parsed and discarded, the server ran forever
# with no concept of scenario completion, and there was no run_id or
# machine-readable pass/fail. `run_scenario()` closes that gap by acting as
# the scenario's own "reasonable client" -- no M3Undle, no external test
# harness, no curl script -- so any public scenario in scenarios/core/ (and
# public CI) can be proven end to end from the engine alone.

DEFAULT_RUN_SETTLE_SECONDS = 2.0
DEFAULT_RUN_CLIENT_TIMEOUT_SECONDS = 5.0
DEFAULT_RUN_MAX_DURATION_SECONDS = 90.0


def _run_mode_stream_urls(playlist_body: str) -> list[str]:
    return [line.strip() for line in playlist_body.splitlines() if line.strip().startswith(("http://", "https://"))]


def _run_mode_drain_url(url: str, *, timeout_s: float) -> None:
    """
    Read one stream URL to completion (or until the connection drops).
    Exceptions are swallowed deliberately: an injected HTTP error
    (baseline #8/#9), a dropped/reset connection (baseline #5, disconnect/
    reset_before_headers), or a stall are often the fault *under test*, not
    a driver failure. Whether the scenario passed is decided afterward from
    the emitted event log against expected_simulator_events, never from
    what this client observed on the wire.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            while response.read(65536):
                pass
    except Exception:
        return


def _run_mode_drain_hls(url: str, *, timeout_s: float) -> None:
    """Fetch an HLS manifest and drain its first segment -- enough to
    exercise the manifest+segment path the same way a real HLS client
    would, without re-downloading every segment of a long playlist."""
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            manifest = response.read().decode("utf-8", errors="replace")
    except Exception:
        return
    base = url.rsplit("/", 1)[0]
    for line in manifest.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            _run_mode_drain_url(f"{base}/{line}", timeout_s=timeout_s)
            return


def _drive_scenario_client(state: ProviderState, base_url: str, *, timeout_s: float, deadline: float) -> None:
    """
    The run mode's built-in "reasonable client": fetches /playlist.m3u using
    the same credentials a real downstream client would already be
    configured with (provider.authentication, when set -- mirroring exactly
    how handle_playlist() embeds them into each stream URL), then reads
    every listed stream URL to completion in parallel. `deadline` is an
    absolute time.monotonic() bound (derived from the scenario's
    run.max_duration_s) shared across every join -- a scenario that never
    settles doesn't hang the run indefinitely.
    """
    playlist_url = f"{base_url}/playlist.m3u"
    if state.auth is not None:
        playlist_url += f"?username={state.auth.username}&password={state.auth.password}"
    try:
        with urllib.request.urlopen(playlist_url, timeout=timeout_s) as response:
            playlist_body = response.read().decode("utf-8", errors="replace")
    except Exception:
        state.log_event("run_mode_playlist_fetch_failed", url=playlist_url)
        return

    threads: list[threading.Thread] = []
    for url in _run_mode_stream_urls(playlist_body):
        drain = _run_mode_drain_hls if url.endswith(".m3u8") else _run_mode_drain_url
        thread = threading.Thread(target=functools.partial(drain, url, timeout_s=timeout_s), daemon=True)
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))


def _normalize_expected_event(entry: str | dict[str, Any]) -> tuple[str, str | None]:
    """A bare string is an event type with no action_id constraint (the
    original shape). An object additionally pins the matched event's
    action_id."""
    if isinstance(entry, str):
        return entry, None
    return str(entry["event"]), entry.get("action_id")


def _match_expected_events(
    events: list[dict[str, Any]], expected: list[str | dict[str, Any]]
) -> tuple[bool, list[dict[str, Any]]]:
    """
    Stage A5's event-API decision said matching should be on event type +
    tagged action_id; this implements both halves now -- ordered-subsequence
    matching, the same semantics the throwaway Stage A5 prototype used
    experimentally. Each expected entry must occur, in order, somewhere in
    the emitted stream; other events may interleave freely. An entry may be
    a bare event-type string (action_id unconstrained) or an object
    ``{"event": ..., "action_id": ...}`` that additionally requires the
    matched event's action_id to equal the given value -- a type match with
    the wrong action_id does not stop the scan; it keeps looking forward for
    a later event of the same type that does carry the right id.
    """
    outcomes: list[dict[str, Any]] = []
    search_from = 0
    all_matched = True
    for raw_expected in expected:
        expected_type, expected_action_id = _normalize_expected_event(raw_expected)
        match = None
        for index in range(search_from, len(events)):
            candidate = events[index]
            if candidate["event"] != expected_type:
                continue
            if expected_action_id is not None and candidate.get("action_id") != expected_action_id:
                continue
            match = candidate
            search_from = index + 1
            break
        if match is None:
            all_matched = False
            outcomes.append({"event": expected_type, "matched": False})
        else:
            outcomes.append({
                "event": expected_type,
                "matched": True,
                "sequence": match["sequence"],
                "virtual_time_ms": match["virtual_time_ms"],
                "action_id": match.get("action_id"),
            })
    return all_matched, outcomes


def _build_run_result(state: ProviderState, path: Path, *, passed: bool, outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    events = list(state.events)
    sequence_aborted = any(entry["event"] == "scenario_sequence_aborted" for entry in events)
    unmatched = [outcome["event"] for outcome in outcomes if not outcome["matched"]]
    return {
        "run_id": state.run_id,
        "scenario_id": state.scenario_id,
        "scenario_path": str(path),
        **report_version(),
        "passed": passed,
        "sequence_aborted": sequence_aborted,
        "failure_reason": None if passed else f"expected event(s) never observed: {', '.join(unmatched)}",
        "expected_events": outcomes,
        "counters": {
            "chunks_sent_by_channel": dict(state.chunks_sent_by_channel),
            "bytes_sent_by_channel": dict(state.bytes_sent_by_channel),
            "total_opened": state.total_opened,
            "total_closed": state.total_closed,
            "total_rejected": state.total_rejected,
            "total_broken": state.total_broken,
        },
        "connection_timeline": dict(state.connection_timeline),
    }


def run_scenario(
    path: Path,
    *,
    run_id: str | None = None,
    bind: str = "127.0.0.1",
    events_file: Path | None = None,
    result_file: Path | None = None,
    settle_seconds: float = DEFAULT_RUN_SETTLE_SECONDS,
    client_timeout_s: float = DEFAULT_RUN_CLIENT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """
    Stage A8.5 entry point: execute `path` to completion against the
    engine's own built-in client, evaluate expected_simulator_events as a
    real contract, and return the machine-readable result dict (also
    written to `result_file` if given). Binds an OS-assigned ephemeral
    local port -- self-contained, no port coordination needed for CI to run
    many scenarios back to back.
    """
    state = load_scenario(path, override_run_id=run_id)

    events_sink = events_file.open("w", encoding="utf-8") if events_file is not None else None
    if events_sink is not None:
        state.set_events_sink(events_sink)

    try:
        server = ProviderServer((bind, 0), ProviderHandler, state, "http://placeholder")
        port = server.server_address[1]
        server.public_base_url = f"http://{bind}:{port}"
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            state.log_event(
                "provider_started",
                bind=bind, port=port, public_base_url=server.public_base_url,
                fixture=str(path), max_streams=state.max_streams, channels=sorted(state.channels),
                mode="run",
            )

            max_duration = float(state.run_config.get("max_duration_s", DEFAULT_RUN_MAX_DURATION_SECONDS))
            deadline = time.monotonic() + max_duration

            runner: ScenarioSequenceRunner | None = None
            if state.sequence:
                runner = ScenarioSequenceRunner(state, state.sequence)
                trigger_timeout = state.run_config.get("trigger_timeout_s")
                if trigger_timeout is not None:
                    runner.trigger_timeout_seconds = float(trigger_timeout)
                runner.start()

            _drive_scenario_client(state, server.public_base_url, timeout_s=client_timeout_s, deadline=deadline)

            if runner is not None:
                runner.join(timeout=max(0.0, deadline - time.monotonic()))

            time.sleep(settle_seconds)

            passed, outcomes = _match_expected_events(list(state.events), state.expected_simulator_events)
            result = _build_run_result(state, path, passed=passed, outcomes=outcomes)
            state.log_event("scenario_completed", passed=passed)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)
    finally:
        if events_sink is not None:
            events_sink.close()

    if result_file is not None:
        result_file.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def cli_run(
    path: Path,
    *,
    run_id: str | None,
    bind: str,
    events_file: Path | None,
    result_file: Path | None,
    settle_seconds: float,
) -> int:
    result = run_scenario(
        path,
        run_id=run_id,
        bind=bind,
        events_file=events_file,
        result_file=result_file,
        settle_seconds=settle_seconds,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a lightweight IPTV provider simulator for manual integration testing."
    )
    parser.add_argument(
        "--fixture",
        default=None,
        help="Path to a provider fixture JSON file, or a scenario YAML/JSON file",
    )
    parser.add_argument(
        "--scenario",
        default=None,
        help="Alias for --fixture; accepts a scenario YAML/JSON file (wins over --fixture if both given). "
        "If neither is given, falls back to a baked-in example scenario so `docker run <image>` "
        "works standalone with no arguments.",
    )
    parser.add_argument("--bind", default="127.0.0.1", help="Bind address")
    parser.add_argument("--port", type=int, default=19001, help="Listen port")
    parser.add_argument("--public-host", default=None, help="Public base URL for stream URLs in /playlist.m3u (defaults to http://<bind>:<port>)")
    parser.add_argument("--provider-name", default=None, help="Override the fixture provider name")
    parser.add_argument("--max-streams", type=int, default=None, help="Override the provider stream cap")
    parser.add_argument(
        "--validate",
        default=None,
        metavar="PATH",
        help="Run fail-closed validation against a fixture/scenario file and exit "
        "(0 if valid, 1 otherwise) without starting the server. --bind/--port/etc. are ignored.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print engine and pinned-dependency versions as JSON and exit, "
        "without starting the server. Stage A8's runtime version-report mechanism.",
    )
    parser.add_argument(
        "--run",
        default=None,
        metavar="PATH",
        help="Execute a scenario to completion against the engine's own built-in client "
        "(fetches /playlist.m3u and drains every listed stream URL), evaluate "
        "expected_simulator_events as a checked contract, print a machine-readable result, "
        "and exit 0 (passed) or 1 (failed). Stage A8.5 self-verification mode -- no external "
        "test harness needed. Binds an OS-assigned ephemeral port on --bind; --port/--fixture/"
        "--scenario/--public-host are ignored.",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Override the auto-generated run_id included in every emitted event's envelope "
        "(and in --run's result). Useful for correlating a run with an external CI job.",
    )
    parser.add_argument(
        "--events-file",
        default=None,
        metavar="PATH",
        help="Append every emitted event as a JSONL line to this file, in addition to the "
        "bounded 200-event /debug/state window. Works in both server and --run mode.",
    )
    parser.add_argument(
        "--result-file",
        default=None,
        metavar="PATH",
        help="With --run, also write the machine-readable result JSON to this path.",
    )
    parser.add_argument(
        "--settle-seconds",
        type=float,
        default=DEFAULT_RUN_SETTLE_SECONDS,
        help=f"With --run, how long to wait after the driven client and any sequence finish "
        f"before evaluating expected_simulator_events, letting trailing async events land "
        f"(default {DEFAULT_RUN_SETTLE_SECONDS}s).",
    )
    return parser.parse_args()


def run_validate(path: Path) -> int:
    """The --validate entry point: load_scenario() already runs the full
    fail-closed validation (structural schema + semantic checks) as part of
    loading, so this just calls it and reports the outcome without binding
    a port."""
    try:
        load_scenario(path)
    except SystemExit as exc:
        print(f"INVALID: {exc}")
        return 1
    print(f"OK: {path}")
    return 0


def report_version() -> dict[str, Any]:
    """
    Stage A8's "runtime version-report script" -- a --version flag on the
    engine itself rather than a separate script file, since the report and
    the engine are the same deployable artifact. Reads dependency versions
    live from the installed packages (not hardcoded here a second time)
    so this can never drift from what docker/simulator/requirements.txt
    actually resolved to at build time.
    """
    return {
        "engine_version": ".".join(str(p) for p in ENGINE_VERSION),
        "supported_schema_versions": sorted(SUPPORTED_SCHEMA_VERSIONS),
        "python_version": platform.python_version(),
        "dependencies": {
            "pyyaml": yaml.__version__,
            "jsonschema": importlib_metadata.version("jsonschema"),
        },
    }


def main() -> None:
    ensure_layout()
    args = parse_args()
    if args.version:
        print(json.dumps(report_version()))
        return
    if args.validate is not None:
        raise SystemExit(run_validate(Path(args.validate)))
    if args.run is not None:
        raise SystemExit(cli_run(
            Path(args.run),
            run_id=args.run_id,
            bind=args.bind,
            events_file=Path(args.events_file) if args.events_file else None,
            result_file=Path(args.result_file) if args.result_file else None,
            settle_seconds=args.settle_seconds,
        ))
    if args.scenario:
        config_path = Path(args.scenario)
    elif args.fixture:
        config_path = Path(args.fixture)
    else:
        config_path = Path(__file__).resolve().parent.parent / "scenarios" / "core" / "baseline-01-clean-continuous-live-stream.yaml"
    state = load_scenario(
        config_path,
        override_provider_name=args.provider_name,
        override_max_streams=args.max_streams,
        override_run_id=args.run_id,
    )
    events_sink = Path(args.events_file).open("w", encoding="utf-8") if args.events_file else None
    if events_sink is not None:
        state.set_events_sink(events_sink)
    public_base_url = args.public_host or f"http://{args.bind}:{args.port}"
    server = ProviderServer((args.bind, args.port), ProviderHandler, state, public_base_url)

    startup_fields: dict[str, Any] = {
        "bind": args.bind,
        "port": args.port,
        "public_base_url": public_base_url,
        "playlist_url": f"{public_base_url}/playlist.m3u",
        "fixture": str(config_path),
        "max_streams": state.max_streams,
        "channels": sorted(state.channels),
    }
    if state.epg:
        startup_fields["epg_url"] = f"{public_base_url}{state.epg.path}"
    if state.xtream:
        startup_fields["xtream_url"] = f"{public_base_url}/player_api.php"
        startup_fields["xtream_username"] = state.xtream.username

    # Routed through log_event() (rather than a bare print()) so startup is
    # queryable via /debug/state's recent_events -- a scenario's
    # expected_simulator_events can now include provider_started.
    state.log_event("provider_started", **startup_fields)

    if state.sequence:
        ScenarioSequenceRunner(state, state.sequence).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        state.log_event("provider_stopped")
    finally:
        server.server_close()
        if events_sink is not None:
            events_sink.close()


if __name__ == "__main__":
    main()
