#!/usr/bin/env python3
"""Bounded Queue Doctor full-ring metadata and pointer-view helpers."""

from __future__ import print_function

import json
import os
import re
import stat


HEADER_BYTES = 64 * 1024
MAX_COMPANION_BYTES = 8 * 1024 * 1024
MAX_POINTER_SCAN_BYTES = 64 * 1024 * 1024
MAX_POINTER_SCAN_LINES = 500000
DEFAULT_SLOT_CONTEXT = 24
ALLOWED_SLOT_CONTEXTS = (16, 24, 32)
MAX_SLOT_BLOCK_LINES = 512
MAX_SLOT_LINE_CHARS = 16384
MAX_U64 = (1 << 64) - 1

RING_BASE_RE = re.compile(r"^\s*Ring base:\s*0x([0-9a-fA-F]+)\s*$")
RING_SIZE_RE = re.compile(
    r"^\s*Ring size:\s*(\d+)\s+bytes\s+\((\d+)\s+packet slots\)\s*$"
)
READ_INDEX_RE = re.compile(
    r"^\s*Read index:\s*(\d+)\s+\(slot\s+(\d+)\)"
    r"(?:\s+\[@\s+([^\]]+)\])?\s*$"
)
WRITE_INDEX_RE = re.compile(
    r"^\s*Write index:\s*(\d+)\s+\(slot\s+(\d+)\)"
    r"(?:\s+\[@\s+([^\]]+)\])?\s*$"
)
POINTER_SOURCE_RE = re.compile(r"^\s*Pointer source:\s*(.+?)\s*$")
ATOMICITY_RE = re.compile(r"^\s*Capture atomicity:\s*(.+?)\s*$")
CAPTURE_STARTED_RE = re.compile(r"^\s*Capture started:\s*(.+?)\s*$")
CAPTURE_FINISHED_RE = re.compile(r"^\s*Capture finished:\s*(.+?)\s*$")
USER_QUEUE_RE = re.compile(
    r"^\s*UMR user-queue context:\s*.*?\bqueue=(\d+)\b"
)
QUEUE_HEADER_RE = re.compile(r"\bQID\s+(\d+)\b")
ARTIFACT_RE = re.compile(
    r"snapshot-(?P<snapshot>\d+)-queue-(?P<queue>\d+)-.+?\.decoded\.txt$",
    re.IGNORECASE,
)
SLOT_LINE_RE = re.compile(
    r"^\[0x[0-9a-fA-F]+@0x(?P<base>[0-9a-fA-F]+)\s+\+\s+"
    r"0x(?P<offset>[0-9a-fA-F]+)\].*?\bOpcode\b"
)
STATE_QUEUE_RE = re.compile(r"^\s*Queue\s+#(?P<queue>\d+):\s*$")
STATE_BASE_RE = re.compile(r"^\s*hqd_base_addr:\s*0x([0-9a-fA-F]+)\s*$")
STATE_RPTR_RE = re.compile(
    r"^\s*hqd_rptr_addr:\s*(?P<address>\S+)\s+\(0x(?P<value>[0-9a-fA-F]+)\)"
)
STATE_WPTR_RE = re.compile(
    r"^\s*rb_wptr_poll_addr:\s*(?P<address>\S+)\s+\(0x(?P<value>[0-9a-fA-F]+)\)"
)
STATE_SIZE_RE = re.compile(r"^\s*rb_buf_size:\s*0x([0-9a-fA-F]+)\s*$")


def _warning(code, message):
    return {"code": code, "message": message}


def _safe_relative(root, relative, maximum_bytes):
    if not relative or os.path.isabs(relative):
        return None
    parts = relative.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    root = os.path.realpath(os.path.abspath(root))
    current = root
    for part in parts:
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except OSError:
            return None
        if stat.S_ISLNK(info.st_mode):
            return None
    try:
        if os.path.commonpath((root, os.path.realpath(current))) != root:
            return None
    except ValueError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum_bytes:
        return None
    return current


def _read_relative_text(root, relative, maximum_bytes=MAX_COMPANION_BYTES):
    path = _safe_relative(root, relative, maximum_bytes)
    if path is None:
        return None
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum_bytes:
            os.close(descriptor)
            return None
        with os.fdopen(
            descriptor, "r", encoding="utf-8", errors="replace"
        ) as stream:
            text = stream.read(maximum_bytes + 1)
    except (IOError, OSError):
        return None
    return text if len(text.encode("utf-8", errors="replace")) <= maximum_bytes else None


def _parse_nonnegative(text):
    try:
        value = int(text, 0)
    except (TypeError, ValueError):
        return None
    return value if 0 <= value <= MAX_U64 else None


def _artifact_identity(relative):
    match = ARTIFACT_RE.match(os.path.basename(relative))
    if match is None:
        return None, None
    return int(match.group("snapshot")), int(match.group("queue"))


def _queue_doctor_root(relative):
    parts = relative.replace("\\", "/").split("/")
    try:
        index = parts.index("full-ring")
    except ValueError:
        return None
    if index == 0:
        return None
    return "/".join(parts[:index])


def _parse_header(text, relative):
    lines = text.splitlines()
    snapshot, filename_queue = _artifact_identity(relative)
    metadata = {
        "source": relative,
        "snapshot": snapshot,
        "queue_id": filename_queue,
        "ring_base": None,
        "ring_size_bytes": None,
        "slot_count": None,
        "rptr": None,
        "wptr": None,
        "declared_rptr_slot": None,
        "declared_wptr_slot": None,
        "rptr_register_address": None,
        "wptr_register_address": None,
        "pointer_source": None,
        "atomicity": None,
        "capture_started": None,
        "capture_finished": None,
        "self_contained": False,
        "companion_status": "not-needed",
        "warnings": [],
        "evidence": [{"path": relative, "line": 1, "label": "Decoded full ring"}],
    }
    structural_markers = {
        "full_dump": False,
        "slot_line": False,
        "decoded_slots": False,
    }
    source_lines = {}
    for line_number, line in enumerate(lines, 1):
        match = RING_BASE_RE.match(line)
        if match:
            metadata["ring_base"] = int(match.group(1), 16)
            source_lines["ring_base"] = line_number
            continue
        match = RING_SIZE_RE.match(line)
        if match:
            metadata["ring_size_bytes"] = int(match.group(1))
            metadata["slot_count"] = int(match.group(2))
            source_lines["capacity"] = line_number
            continue
        match = READ_INDEX_RE.match(line)
        if match:
            metadata["rptr"] = int(match.group(1))
            metadata["declared_rptr_slot"] = int(match.group(2))
            metadata["rptr_register_address"] = match.group(3)
            source_lines["rptr"] = line_number
            continue
        match = WRITE_INDEX_RE.match(line)
        if match:
            metadata["wptr"] = int(match.group(1))
            metadata["declared_wptr_slot"] = int(match.group(2))
            metadata["wptr_register_address"] = match.group(3)
            source_lines["wptr"] = line_number
            continue
        match = POINTER_SOURCE_RE.match(line)
        if match:
            metadata["pointer_source"] = match.group(1)
            source_lines["pointer_source"] = line_number
            continue
        match = ATOMICITY_RE.match(line)
        if match:
            metadata["atomicity"] = match.group(1)
            source_lines["atomicity"] = line_number
            continue
        match = CAPTURE_STARTED_RE.match(line)
        if match:
            metadata["capture_started"] = match.group(1)
            continue
        match = CAPTURE_FINISHED_RE.match(line)
        if match:
            metadata["capture_finished"] = match.group(1)
            continue
        match = USER_QUEUE_RE.match(line)
        if match:
            parsed_queue = int(match.group(1))
            if metadata["queue_id"] is None:
                metadata["queue_id"] = parsed_queue
            elif metadata["queue_id"] != parsed_queue:
                metadata["warnings"].append(
                    _warning(
                        "QUEUE_ID_CONFLICT",
                        "Filename and decoded-header queue identities disagree.",
                    )
                )
            continue
        if metadata["queue_id"] is None:
            queue_header = QUEUE_HEADER_RE.search(line)
            if queue_header:
                metadata["queue_id"] = int(queue_header.group(1))
        if "Full physical-slot dump." in line:
            structural_markers["full_dump"] = True
        if line.strip().startswith("Decoded slots:"):
            structural_markers["decoded_slots"] = True
        if SLOT_LINE_RE.match(line):
            structural_markers["slot_line"] = True

    recognized = (
        metadata["ring_base"] is not None
        and metadata["ring_size_bytes"] is not None
        and structural_markers["slot_line"]
        and (structural_markers["full_dump"] or structural_markers["decoded_slots"])
    )
    if not recognized:
        return None
    metadata["source_lines"] = source_lines
    metadata["self_contained"] = (
        metadata["rptr"] is not None
        and metadata["wptr"] is not None
        and metadata["pointer_source"] is not None
        and metadata["atomicity"] is not None
    )
    return metadata


def _state_candidates(text, source, snapshot):
    candidates = []
    current = None
    for line_number, line in enumerate((text or "").splitlines(), 1):
        match = STATE_QUEUE_RE.match(line)
        if match:
            if current:
                candidates.append(current)
            current = {
                "snapshot": snapshot,
                "queue_id": int(match.group("queue")),
                "ring_base": None,
                "ring_size_bytes": None,
                "rptr": None,
                "wptr": None,
                "pointer_source": "--print-uq hqd_rptr_addr/rb_wptr_poll_addr values",
                "atomicity": (
                    "pointer pair from one --print-uq queue-state capture; "
                    "relationship to decoded ring bytes is non-atomic"
                ),
                "source": source,
                "line": line_number,
            }
            continue
        if current is None:
            continue
        match = STATE_BASE_RE.match(line)
        if match:
            current["ring_base"] = int(match.group(1), 16)
            continue
        match = STATE_RPTR_RE.match(line)
        if match:
            current["rptr"] = int(match.group("value"), 16)
            current["rptr_register_address"] = match.group("address")
            continue
        match = STATE_WPTR_RE.match(line)
        if match:
            current["wptr"] = int(match.group("value"), 16)
            current["wptr_register_address"] = match.group("address")
            continue
        match = STATE_SIZE_RE.match(line)
        if match:
            current["ring_size_bytes"] = int(match.group(1), 16) * 4
    if current:
        candidates.append(current)
    return candidates


def _walk_json_artifacts(value, snapshot=None):
    records = []
    if isinstance(value, dict):
        if "full_ring_artifacts" in value and isinstance(
            value["full_ring_artifacts"], list
        ):
            for artifact in value["full_ring_artifacts"]:
                if isinstance(artifact, dict):
                    record = dict(artifact)
                    record.setdefault("snapshot", value.get("index", snapshot))
                    records.append(record)
        for key, child in value.items():
            child_snapshot = snapshot
            if key == "snapshots" and isinstance(child, list):
                for index, item in enumerate(child):
                    records.extend(_walk_json_artifacts(item, index))
                continue
            records.extend(_walk_json_artifacts(child, child_snapshot))
    elif isinstance(value, list):
        for child in value:
            records.extend(_walk_json_artifacts(child, snapshot))
    return records


def _json_candidates(text, source, relative):
    try:
        report = json.loads(text)
    except (TypeError, ValueError):
        return []
    basename = os.path.basename(relative)
    candidates = []
    for artifact in _walk_json_artifacts(report):
        artifact_path = artifact.get("decoded_path") or artifact.get("path")
        if not artifact_path or os.path.basename(str(artifact_path)) != basename:
            continue
        context = artifact.get("capture_context") or {}
        candidate = {
            "snapshot": artifact.get("snapshot"),
            "queue_id": artifact.get("queue_id", context.get("umr_queue_id")),
            "ring_base": artifact.get("ring_base"),
            "ring_size_bytes": artifact.get("ring_size_bytes"),
            "rptr": artifact.get("capture_rptr"),
            "wptr": artifact.get("capture_wptr"),
            "pointer_source": artifact.get("pointer_source"),
            "atomicity": (
                artifact.get("pending_correlation") or {}
            ).get("atomicity"),
            "source": source,
            "line": 1,
        }
        candidates.append(candidate)
    return candidates


def _candidate_key(candidate):
    return (
        candidate.get("snapshot"),
        candidate.get("queue_id"),
        candidate.get("ring_base"),
        candidate.get("ring_size_bytes"),
        candidate.get("rptr"),
        candidate.get("wptr"),
    )


def _merge_companions(metadata, root):
    queue_root = _queue_doctor_root(metadata["source"])
    if queue_root is None:
        metadata["companion_status"] = "unavailable"
        return
    candidates = []
    snapshot = metadata.get("snapshot")
    queue_id = metadata.get("queue_id")
    if snapshot is not None:
        state_source = "%s/snapshot-%02d-queue-state.txt" % (queue_root, snapshot)
        state_text = _read_relative_text(root, state_source)
        if state_text is not None:
            candidates.extend(_state_candidates(state_text, state_source, snapshot))

    report_source = queue_root + "/report.json"
    report_text = _read_relative_text(root, report_source)
    if report_text is not None:
        candidates.extend(
            _json_candidates(report_text, report_source, metadata["source"])
        )

    summary_source = queue_root + "/summary.txt"
    summary_text = _read_relative_text(root, summary_source)
    if summary_text is not None and os.path.basename(metadata["source"]) in summary_text:
        metadata["evidence"].append(
            {"path": summary_source, "line": 1, "label": "Queue Doctor summary"}
        )

    scoped = []
    for candidate in candidates:
        if snapshot is None or queue_id is None:
            continue
        if candidate.get("snapshot") != snapshot or candidate.get("queue_id") != queue_id:
            continue
        known_base = metadata.get("ring_base")
        candidate_base = candidate.get("ring_base")
        if known_base is None or candidate_base is None or known_base != candidate_base:
            continue
        known_size = metadata.get("ring_size_bytes")
        candidate_size = candidate.get("ring_size_bytes")
        if (
            known_size is not None
            and candidate_size is not None
            and known_size != candidate_size
        ):
            continue
        scoped.append(candidate)

    deduplicated = {}
    for candidate in scoped:
        key = _candidate_key(candidate)
        existing = deduplicated.get(key)
        if existing is None:
            candidate = dict(candidate)
            candidate["sources"] = [
                {
                    "path": candidate.get("source"),
                    "line": candidate.get("line"),
                    "label": "Pointer companion metadata",
                }
            ]
            deduplicated[key] = candidate
        else:
            existing["sources"].append(
                {
                    "path": candidate.get("source"),
                    "line": candidate.get("line"),
                    "label": "Pointer companion metadata",
                }
            )
    unique = list(deduplicated.values())
    if len(unique) != 1:
        metadata["companion_status"] = (
            "ambiguous" if len(unique) > 1 else "unmatched"
        )
        metadata["warnings"].append(
            _warning(
                "AMBIGUOUS_COMPANION_METADATA"
                if len(unique) > 1
                else "COMPANION_METADATA_UNMATCHED",
                (
                    "Companion pointer metadata was rejected because multiple "
                    "snapshot/queue/base matches disagree."
                    if len(unique) > 1
                    else (
                        "No companion pointer record matched snapshot, queue, "
                        "ring base, and capacity."
                    )
                ),
            )
        )
        return

    candidate = unique[0]
    for field in (
        "rptr",
        "wptr",
        "pointer_source",
        "atomicity",
        "rptr_register_address",
        "wptr_register_address",
    ):
        if metadata.get(field) is None and candidate.get(field) is not None:
            metadata[field] = candidate[field]
    metadata["evidence"].extend(candidate["sources"])
    metadata["companion_status"] = "exact"


def _derive(metadata):
    size = metadata.get("ring_size_bytes")
    declared_slots = metadata.get("slot_count")
    base = metadata.get("ring_base")
    if (
        size is None
        or size <= 0
        or size > MAX_U64
        or size % 64
        or base is None
        or base < 0
        or base > MAX_U64
    ):
        metadata["warnings"].append(
            _warning(
                "INVALID_RING_GEOMETRY",
                "Ring base/capacity is unavailable or invalid for 64-byte AQL slots.",
            )
        )
        metadata["geometry_valid"] = False
        metadata["rptr_view"] = None
        metadata["wptr_view"] = None
        return metadata
    slot_count = size // 64
    if declared_slots is not None and declared_slots != slot_count:
        metadata["warnings"].append(
            _warning(
                "RING_CAPACITY_MISMATCH",
                "Declared packet-slot count disagrees with byte capacity.",
            )
        )
        metadata["geometry_valid"] = False
        metadata["rptr_view"] = None
        metadata["wptr_view"] = None
        return metadata
    metadata["slot_count"] = slot_count
    metadata["geometry_valid"] = True

    for role in ("rptr", "wptr"):
        logical = metadata.get(role)
        if logical is None or logical < 0 or logical > MAX_U64:
            metadata[role + "_view"] = None
            continue
        slot = logical % slot_count
        offset = slot * 64
        address = base + offset
        if address > MAX_U64:
            metadata[role + "_view"] = None
            metadata["warnings"].append(
                _warning(
                    "POINTER_ADDRESS_OVERFLOW",
                    "%s ring address arithmetic overflowed." % role.upper(),
                )
            )
            continue
        declared = metadata.get("declared_" + role + "_slot")
        if declared is not None and declared != slot:
            metadata["warnings"].append(
                _warning(
                    role.upper() + "_SLOT_MISMATCH",
                    "Declared %s slot disagrees with logical index modulo capacity."
                    % role.upper(),
                )
            )
            metadata[role + "_view"] = None
            continue
        metadata[role + "_view"] = {
            "logical": logical,
            "slot": slot,
            "byte_offset": offset,
            "address": address,
        }

    rptr = metadata.get("rptr")
    wptr = metadata.get("wptr")
    metadata["pending_count"] = None
    metadata["wraps"] = None
    if (
        rptr is not None
        and wptr is not None
        and metadata.get("rptr_view")
        and metadata.get("wptr_view")
    ):
        if rptr == wptr:
            pending_count = 0
        elif wptr > rptr:
            pending_count = wptr - rptr
        else:
            pending_count = (
                metadata["wptr_view"]["slot"] - metadata["rptr_view"]["slot"]
            ) % slot_count
            if pending_count == 0:
                pending_count = slot_count
        metadata["pending_count"] = pending_count
        metadata["wraps"] = (
            wptr < rptr
            or metadata["rptr_view"]["slot"] + pending_count > slot_count
        )
        if pending_count > slot_count:
            metadata["warnings"].append(
                _warning(
                    "PENDING_EXCEEDS_CAPACITY",
                    "Logical pending interval exceeds physical ring capacity.",
                )
            )
    return metadata


def inspect_full_ring(stream, root, relative):
    """Detect and parse one decoded full ring, restoring the stream position."""
    try:
        stream.seek(0)
        header = stream.read(HEADER_BYTES).decode("utf-8", errors="replace")
        stream.seek(0)
    except (IOError, OSError):
        return None
    metadata = _parse_header(header, relative)
    if metadata is None:
        return None
    if not metadata["self_contained"]:
        metadata["companion_status"] = "searching"
        _merge_companions(metadata, root)
    return _derive(metadata)


def pointer_roles_for_lines(lines, line_numbers, metadata):
    """Return source-line roles for pointer slot header lines in a preview."""
    targets = {}
    for role in ("rptr", "wptr"):
        view = metadata.get(role + "_view")
        if view:
            targets.setdefault(view["byte_offset"], set()).add(role)
    roles = {}
    for line, line_number in zip(lines, line_numbers):
        match = SLOT_LINE_RE.match(line)
        if not match:
            continue
        if int(match.group("base"), 16) != metadata.get("ring_base"):
            continue
        line_roles = targets.get(int(match.group("offset"), 16))
        if line_roles:
            roles[line_number] = set(line_roles)
    return roles


def read_slot_window(
    stream, metadata, role, center_slot=None, context=DEFAULT_SLOT_CONTEXT
):
    """Read complete circular physical-slot blocks around one pointer or slot."""
    view = metadata.get(role + "_view")
    if view is None:
        return {
            "available": False,
            "reason": "%s metadata is unavailable" % role.upper(),
            "lines": [],
            "line_numbers": [],
            "roles": {},
            "scan_limited": False,
        }
    capacity = metadata.get("slot_count")
    if (
        not isinstance(capacity, int)
        or capacity <= 0
        or context not in ALLOWED_SLOT_CONTEXTS
    ):
        return {
            "available": False,
            "reason": "Ring capacity or slot-window context is invalid.",
            "lines": [],
            "line_numbers": [],
            "roles": {},
            "scan_limited": False,
        }
    if center_slot is None:
        center_slot = view["slot"]
    if not isinstance(center_slot, int) or not 0 <= center_slot < capacity:
        return {
            "available": False,
            "reason": "Requested physical slot is outside the ring.",
            "lines": [],
            "line_numbers": [],
            "roles": {},
            "scan_limited": False,
        }

    total_slots = min(capacity, context * 2 + 1)
    before_count = min(context, (total_slots - 1) // 2)
    after_count = total_slots - before_count - 1
    desired_slots = [
        (center_slot - before_count + offset) % capacity
        for offset in range(total_slots)
    ]
    desired_set = set(desired_slots)
    blocks = {}
    current_slot = None
    current_block = []
    oversized_slots = set()
    bytes_read = 0
    scan_limited = False

    def finish_block():
        if current_slot is None:
            return
        if len(current_block) <= MAX_SLOT_BLOCK_LINES:
            blocks[current_slot] = list(current_block)
        else:
            oversized_slots.add(current_slot)

    try:
        stream.seek(0)
        for line_number, raw in enumerate(stream, 1):
            bytes_read += len(raw)
            if (
                bytes_read > MAX_POINTER_SCAN_BYTES
                or line_number > MAX_POINTER_SCAN_LINES
            ):
                scan_limited = True
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if len(line) > MAX_SLOT_LINE_CHARS:
                line = line[:MAX_SLOT_LINE_CHARS] + "\u2026 [line clipped]"
            match = SLOT_LINE_RE.match(line)
            if match is not None:
                finish_block()
                current_slot = None
                current_block = []
                base = int(match.group("base"), 16)
                offset = int(match.group("offset"), 16)
                if (
                    base == metadata.get("ring_base")
                    and offset % 64 == 0
                    and offset // 64 in desired_set
                ):
                    current_slot = offset // 64
                    current_block = [(line_number, line)]
                continue
            if current_slot is not None:
                current_block.append((line_number, line))
        else:
            finish_block()
    except (IOError, OSError):
        blocks = {}
    finally:
        try:
            stream.seek(0)
        except (IOError, OSError):
            pass
    if center_slot not in blocks:
        return {
            "available": False,
            "reason": (
                "Selected physical slot block was not found in the bounded scan."
            ),
            "lines": [],
            "line_numbers": [],
            "roles": {},
            "scan_limited": scan_limited,
        }

    selected = []
    rendered_slots = []
    for slot in desired_slots:
        block = blocks.get(slot)
        if block is None:
            continue
        selected.extend(block)
        rendered_slots.append(slot)
    lines = [item[1] for item in selected]
    numbers = [item[0] for item in selected]
    roles = pointer_roles_for_lines(lines, numbers, metadata)
    center_line = blocks[center_slot][0][0]
    pointer_slot = view["slot"]
    logical_delta = (center_slot - pointer_slot) % capacity
    if logical_delta > capacity // 2:
        logical_delta -= capacity
    logical_center = view["logical"] + logical_delta
    start_slot = desired_slots[0]
    end_slot = desired_slots[-1]
    return {
        "available": True,
        "reason": None,
        "lines": lines,
        "line_numbers": numbers,
        "roles": roles,
        "scan_limited": scan_limited,
        "target_line": center_line,
        "window": {
            "center_slot": center_slot,
            "center_line": center_line,
            "context": context,
            "before_count": before_count,
            "after_count": after_count,
            "start_slot": start_slot,
            "end_slot": end_slot,
            "wraps": start_slot > end_slot or total_slots == capacity,
            "requested_slot_count": total_slots,
            "rendered_slot_count": len(rendered_slots),
            "missing_slots": [
                slot for slot in desired_slots if slot not in rendered_slots
            ],
            "oversized_slots": sorted(oversized_slots),
            "logical_center": logical_center,
            "logical_start": logical_center - before_count,
            "logical_end": logical_center + after_count,
            "previous_center": (center_slot - total_slots) % capacity,
            "next_center": (center_slot + total_slots) % capacity,
            "slot_sequence": rendered_slots,
        },
    }


def read_pointer_neighborhood(stream, metadata, role):
    """Backward-compatible pointer-centered complete-slot window."""
    return read_slot_window(stream, metadata, role)
