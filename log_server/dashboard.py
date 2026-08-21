#!/usr/bin/env python3
"""Bounded, reusable GPU debug-bundle analysis and dashboard rendering."""

from __future__ import print_function

import collections
import datetime
import html
import json
import os
import re
import stat
import sys
import threading
from urllib.parse import quote


DEBUG_SCRIPTS_DIRECTORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if DEBUG_SCRIPTS_DIRECTORY not in sys.path:
    sys.path.insert(0, DEBUG_SCRIPTS_DIRECTORY)

from cpc_parser import parse_cpc  # noqa: E402


MAX_CPC_LOGS = 128
MAX_CPC_LOG_BYTES = 4 * 1024 * 1024
MAX_TEXT_SOURCE_BYTES = 8 * 1024 * 1024
MAX_JSON_SOURCE_BYTES = 4 * 1024 * 1024
MAX_SIGNATURE_FILES = 2048

GPU_MAP_RE = re.compile(
    r"^\s*#\s*gpu=(?P<node>\d+)\s+amd_smi_gpu=(?P<amd>\d+)\s+"
    r"pci_bdf=(?P<bdf>[0-9a-fA-F:.]+)\s*$"
)
KFD_NODE_RE = re.compile(
    r"^\s*Node\s+(?P<node>\d+),\s*gpu_id\s+(?P<gpu>[0-9a-fA-F]+):"
)
QUEUE_SNAPSHOT_RE = re.compile(
    r"^\s*umr_queue=(?P<queue>\d+)\s+type=(?P<type>[^\s]+)\s+"
    r"rptr=0x(?P<rptr>[0-9a-fA-F]+)\s+wptr=0x(?P<wptr>[0-9a-fA-F]+)\s+"
    r"state=(?P<state>[^\s]+)\s+kfd_gpu_id=(?P<gpu>\d+)"
)
QUEUE_LIST_RE = re.compile(
    r"^\s*(?:\*\*)?\s*queue=(?P<queue>\d+)\s+type=(?P<type>\d+)\s+"
    r"mqd_gpu_addr=0x(?P<mqd>[0-9a-fA-F]+)\s+"
    r"rptr=0x(?P<rptr>[0-9a-fA-F]+)\s+wptr=0x(?P<wptr>[0-9a-fA-F]+)"
)
PACKET_TYPE_RE = re.compile(r"\[HSA_(?P<type>[A-Z0-9_]+)\]")
VM_PARTITION_RE = re.compile(r"(?:--vm-partition|-vmp)\s+(?P<xcc>\d+)")
NO_PROGRESS_RE = re.compile(r"QUEUE_NO_PROGRESS\s+\(queue=(?P<queue>\d+)\)")
HQD_HEADER_RE = re.compile(
    r"^\s*Inst\s+(?P<xcc>\d+),\s+(?:CP|HIQ).*?"
    r"Pipe\s+(?P<pipe>\d+).*?Queue\s+(?P<queue>\d+)",
    re.IGNORECASE,
)
HQD_WORDS_RE = re.compile(r"^\s*[0-9a-fA-F]+:\s+(?P<words>(?:[0-9a-fA-F]{8}\s+){2,})")
COLLECTOR_ROW_RE = re.compile(
    r"^(?P<name>[a-z0-9_]+)\s+(?P<status>PASS|FAIL|SKIP)\s+(?P<detail>\S+)"
)

_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _hex(value):
    return "0x%x" % value if value is not None else None


def _bounded_file(root, relative_path, maximum_bytes):
    """Resolve one fixed relative source without crossing the configured root."""
    root = os.path.realpath(os.path.abspath(root))
    candidate = os.path.abspath(os.path.join(root, relative_path))
    try:
        if os.path.commonpath((root, candidate)) != root:
            return None
    except ValueError:
        return None
    current = root
    for component in relative_path.replace("\\", "/").split("/"):
        if not component or component in (".", ".."):
            return None
        current = os.path.join(current, component)
        try:
            info = os.lstat(current)
        except OSError:
            return None
        if stat.S_ISLNK(info.st_mode):
            return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum_bytes:
        return None
    return current


def _read_text(root, relative_path, maximum_bytes=MAX_TEXT_SOURCE_BYTES):
    path = _bounded_file(root, relative_path, maximum_bytes)
    if path is None:
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            value = stream.read(maximum_bytes + 1)
            return value if len(value) <= maximum_bytes else None
    except (IOError, OSError):
        return None


def _read_json(root, relative_path):
    text = _read_text(root, relative_path, MAX_JSON_SOURCE_BYTES)
    if text is None:
        return None
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value


def _relative_source(root, path):
    root = os.path.realpath(os.path.abspath(root))
    path = os.path.realpath(os.path.abspath(path))
    try:
        if os.path.commonpath((root, path)) != root:
            return None
    except ValueError:
        return None
    return os.path.relpath(path, root).replace(os.sep, "/")


def source_url(relative_path, line=None):
    if not relative_path or relative_path.startswith("/") or ".." in relative_path.split("/"):
        return None
    is_directory = relative_path.endswith("/")
    parts = relative_path.rstrip("/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    url = "/" + "/".join(quote(part, safe="") for part in parts)
    if is_directory:
        url += "/"
    if line is not None:
        url += "#L%d" % line
    return url


def is_gpu_debug_bundle(root):
    """Conservatively recognize a collected GPU debug bundle."""
    if not os.path.isdir(root):
        return False
    markers = (
        os.path.isfile(os.path.join(root, "summary.txt")),
        os.path.isdir(os.path.join(root, "cpc")),
        os.path.isdir(os.path.join(root, "queue_doctor")),
        os.path.isdir(os.path.join(root, "kfd")),
    )
    return sum(1 for marker in markers if marker) >= 2


def _signature(root):
    records = []
    count = 0
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not os.path.islink(os.path.join(directory, name))
        )
        for filename in sorted(filenames):
            if count >= MAX_SIGNATURE_FILES:
                return tuple(records + [("__truncated__", count, count)])
            path = os.path.join(directory, filename)
            if os.path.islink(path):
                continue
            lowered = filename.lower()
            relevant = (
                "umr_cpc" in lowered
                or lowered in (
                    "summary.txt",
                    "collector.txt",
                    "target-gpus.json",
                    "process-selection.json",
                    "sdma_registers.txt",
                    "system_info.txt",
                    "hqds.txt",
                    "rls.txt",
                )
                or (lowered.startswith("snapshot-") and lowered.endswith("-list-uq.txt"))
                or (
                    "pending" in directory.replace("\\", "/").lower()
                    and lowered.endswith(".txt")
                )
            )
            if not relevant:
                continue
            try:
                info = os.stat(path)
            except OSError:
                continue
            records.append(
                (
                    os.path.relpath(path, root),
                    getattr(info, "st_mtime_ns", int(info.st_mtime * 1000000000)),
                    info.st_size,
                )
            )
            count += 1
    return tuple(records)


def parse_sdma_gpu_mapping(text, source="sdma_registers.txt"):
    """Parse authoritative KFD-node/AMD-SMI/PCI mapping comments."""
    mappings = []
    warnings = []
    if not text:
        return mappings, warnings
    for line_number, line in enumerate(text.splitlines(), 1):
        match = GPU_MAP_RE.match(line)
        if match is None:
            continue
        mappings.append(
            {
                "node_id": int(match.group("node")),
                "amd_smi_id": int(match.group("amd")),
                "pci_bdf": match.group("bdf").lower(),
                "source": source,
                "line": line_number,
            }
        )
    if not mappings:
        warnings.append(
            {
                "code": "GPU_MAPPING_ABSENT",
                "message": "No KFD-node/AMD-SMI/PCI mapping rows were found.",
                "source": source,
            }
        )
    return mappings, warnings


def parse_kfd_nodes(text, source):
    nodes = []
    if not text:
        return nodes
    for line_number, line in enumerate(text.splitlines(), 1):
        match = KFD_NODE_RE.match(line)
        if match is not None:
            nodes.append(
                {
                    "node_id": int(match.group("node")),
                    "gpu_id": int(match.group("gpu"), 16),
                    "source": source,
                    "line": line_number,
                }
            )
    return nodes


def parse_kfd_hqds(text, source, node_to_bdf):
    """Parse KFD HQD slot identity and active MQD address conservatively."""
    records = []
    if not text:
        return records
    current_node = None
    pending = None
    for line_number, line in enumerate(text.splitlines(), 1):
        node_match = KFD_NODE_RE.match(line)
        if node_match is not None:
            current_node = int(node_match.group("node"))
            pending = None
            continue
        header = HQD_HEADER_RE.match(line)
        if header is not None:
            pending = {
                "node_id": current_node,
                "pci_bdf": node_to_bdf.get(current_node),
                "xcc": int(header.group("xcc")),
                "pipe": int(header.group("pipe")),
                "queue": int(header.group("queue")),
                "slot_label": "Inst %s Pipe %s Queue %s"
                % (
                    header.group("xcc"),
                    header.group("pipe"),
                    header.group("queue"),
                ),
                "source": source,
                "line": line_number,
            }
            continue
        if pending is None:
            continue
        words_match = HQD_WORDS_RE.match(line)
        if words_match is None:
            continue
        words = words_match.group("words").split()
        if len(words) < 3:
            pending = None
            continue
        pending["mqd_base"] = (int(words[1], 16) << 32) | int(words[0], 16)
        pending["active"] = bool(int(words[2], 16) & 1)
        records.append(pending)
        pending = None
    return records


def parse_queue_doctor(root, target_metadata):
    """Parse bounded Queue Doctor summaries without loading its huge report JSON."""
    summary_source = "queue_doctor/summary.txt"
    summary = _read_text(root, summary_source)
    queues = {}
    no_progress = set()
    warnings = []
    if summary:
        for line_number, line in enumerate(summary.splitlines(), 1):
            match = QUEUE_SNAPSHOT_RE.match(line)
            if match is not None:
                queue_id = int(match.group("queue"))
                queues.setdefault(
                    queue_id,
                    {
                        "queue_id": queue_id,
                        "snapshots": [],
                        "source": summary_source,
                        "line": line_number,
                    },
                )["snapshots"].append(
                    {
                        "rptr": int(match.group("rptr"), 16),
                        "wptr": int(match.group("wptr"), 16),
                        "state": match.group("state"),
                        "gpu_id": int(match.group("gpu")),
                    }
                )
            progress = NO_PROGRESS_RE.search(line)
            if progress is not None:
                no_progress.add(int(progress.group("queue")))
    else:
        warnings.append(
            {
                "code": "QUEUE_DOCTOR_SUMMARY_ABSENT",
                "message": "Queue Doctor summary is unavailable.",
                "source": summary_source,
            }
        )

    list_directory = os.path.join(root, "queue_doctor")
    list_paths = []
    if os.path.isdir(list_directory):
        for filename in sorted(os.listdir(list_directory)):
            if re.match(r"snapshot-\d+-list-uq\.txt$", filename):
                list_paths.append(os.path.join(list_directory, filename))
    if list_paths:
        latest = list_paths[-1]
        relative = _relative_source(root, latest)
        text = _read_text(root, relative)
        if text:
            for line_number, line in enumerate(text.splitlines(), 1):
                match = QUEUE_LIST_RE.match(line)
                if match is None:
                    continue
                queue_id = int(match.group("queue"))
                item = queues.setdefault(
                    queue_id,
                    {
                        "queue_id": queue_id,
                        "snapshots": [],
                        "source": relative,
                        "line": line_number,
                    },
                )
                item["mqd_base"] = int(match.group("mqd"), 16)
                item["list_source"] = relative
                item["list_line"] = line_number

    pending_directory = os.path.join(root, "queue_doctor", "pending")
    pending_by_queue = {}
    if os.path.isdir(pending_directory):
        for filename in sorted(os.listdir(pending_directory)):
            match = re.match(
                r"snapshot-(?P<snap>\d+)-queue-(?P<queue>\d+)-.*\.txt$", filename
            )
            if match is None:
                continue
            pending_by_queue[int(match.group("queue"))] = os.path.join(
                pending_directory, filename
            )
    for queue_id, path in pending_by_queue.items():
        relative = _relative_source(root, path)
        text = _read_text(root, relative)
        if text is None:
            continue
        packet_types = collections.Counter(
            match.group("type") for match in PACKET_TYPE_RE.finditer(text)
        )
        xcc_match = VM_PARTITION_RE.search(text[:4096])
        item = queues.setdefault(
            queue_id,
            {"queue_id": queue_id, "snapshots": [], "source": relative, "line": 1},
        )
        item["packet_types"] = dict(sorted(packet_types.items()))
        item["packet_count"] = sum(packet_types.values())
        item["pending_source"] = relative
        item["xcc"] = int(xcc_match.group("xcc")) if xcc_match else None

    target_gpu = None
    targets = (target_metadata or {}).get("target_gpus") or []
    if targets:
        target_gpu = targets[0]
    for queue_id, item in queues.items():
        snapshots = item["snapshots"]
        contexts = {
            (snapshot["rptr"], snapshot["wptr"], snapshot["state"])
            for snapshot in snapshots
        }
        item["stable_context"] = len(snapshots) >= 2 and len(contexts) == 1
        latest = snapshots[-1] if snapshots else None
        item["state"] = latest["state"] if latest else "unknown"
        item["rptr"] = latest["rptr"] if latest else None
        item["wptr"] = latest["wptr"] if latest else None
        item["pending_count"] = None
        if (
            latest
            and latest["state"] == "pending"
            and latest["wptr"] >= latest["rptr"]
        ):
            item["pending_count"] = latest["wptr"] - latest["rptr"]
        item["stall_candidate"] = (
            item["stable_context"]
            and item["state"] == "pending"
            and queue_id in no_progress
        )
        item["confidence"] = (
            "repeated same-context Queue Doctor snapshots"
            if item["stall_candidate"]
            else "single/latest Queue Doctor state"
        )
        if target_gpu:
            item["pci_bdf"] = target_gpu.get("pci_bdf", "").lower() or None
            item["gpu_id"] = target_gpu.get("gpu_id")
            item["node_id"] = target_gpu.get("node_id")
    return {
        "queues": [queues[key] for key in sorted(queues)],
        "warnings": warnings,
        "source": summary_source if summary else None,
        "snapshot_count": max(
            [len(item["snapshots"]) for item in queues.values()] or [0]
        ),
        "pending_total": sum(
            item["pending_count"] or 0 for item in queues.values()
        ),
    }


def classify_cpc_queue(queue):
    derived = queue.get("derived") or {}
    if not (derived.get("active") or {}).get("is_active"):
        return "inactive-stale"
    pq = (derived.get("decoded") or {}).get("pq_control")
    if pq is None:
        return "unknown"
    return "empty-active" if pq.get("pq_empty") else "non-empty"


def exact_mqd_join(row, candidates):
    """Join only exact MQD addresses within independently known GPU/XCC scope."""
    address = row.get("mqd_base")
    matches = []
    for candidate in candidates:
        if address is None or candidate.get("mqd_base") != address:
            continue
        row_bdf = row.get("pci_bdf")
        candidate_bdf = candidate.get("pci_bdf")
        if row_bdf and candidate_bdf and row_bdf.lower() != candidate_bdf.lower():
            continue
        row_xcc = row.get("xcc")
        candidate_xcc = candidate.get("xcc")
        if row_xcc is not None and candidate_xcc is not None and row_xcc != candidate_xcc:
            continue
        matches.append(candidate)
    if not matches:
        status = "unmatched"
        confidence = "none"
    elif len(matches) == 1:
        status = "exact"
        confidence = "high"
    else:
        status = "ambiguous"
        confidence = "low"
    return {
        "status": status,
        "confidence": confidence,
        "matches": matches,
        "evidence": "exact MQD_BASE with matching known PCI/XCC context",
    }


def _find_latest(root, filename):
    matches = []
    visited = 0
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if not os.path.islink(os.path.join(directory, name))
        ]
        visited += len(filenames)
        if visited > MAX_SIGNATURE_FILES:
            break
        if filename in filenames:
            path = os.path.join(directory, filename)
            if not os.path.islink(path):
                matches.append(path)
    return sorted(matches)[-1] if matches else None


def _discover_cpc_logs(root):
    """Discover CPC captures under the fixed CPC directory with hard limits."""
    cpc_root = os.path.join(root, "cpc")
    if not os.path.isdir(cpc_root) or os.path.islink(cpc_root):
        return [], cpc_root, False
    paths = []
    visited = 0
    truncated = False
    for directory, dirnames, filenames in os.walk(cpc_root):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not os.path.islink(os.path.join(directory, name))
        )
        for filename in sorted(filenames):
            visited += 1
            if visited > MAX_SIGNATURE_FILES:
                truncated = True
                break
            path = os.path.join(directory, filename)
            if os.path.islink(path):
                continue
            lowered = filename.lower()
            if lowered.endswith(".txt") and "umr_cpc" in lowered:
                paths.append(path)
                if len(paths) >= MAX_CPC_LOGS:
                    truncated = True
                    break
        if truncated:
            break
    return sorted(paths), cpc_root, truncated


def _parse_collector_summary(text):
    statuses = {}
    completed_at = None
    if not text:
        return statuses, completed_at
    for line in text.splitlines():
        match = COLLECTOR_ROW_RE.match(line)
        if match is not None:
            statuses[match.group("name")] = {
                "status": match.group("status"),
                "detail": match.group("detail"),
            }
        elif line.startswith("completed_at="):
            completed_at = line.split("=", 1)[1]
    return statuses, completed_at


def _source_panel(name, status, detail, source=None):
    return {"name": name, "status": status, "detail": detail, "source": source}


def _analyze_uncached(root):
    root = os.path.realpath(os.path.abspath(root))
    warnings = []
    target_metadata = _read_json(root, "queue_doctor/target-gpus.json") or {}
    if not target_metadata:
        warnings.append(
            {
                "code": "TARGET_METADATA_ABSENT",
                "message": "Target GPU inventory is unavailable; target joins are limited.",
            }
        )

    sdma_text = _read_text(root, "sdma_registers.txt")
    mappings, mapping_warnings = parse_sdma_gpu_mapping(sdma_text)
    warnings.extend(mapping_warnings)
    node_to_bdf = {item["node_id"]: item["pci_bdf"] for item in mappings}
    bdf_to_mapping = {item["pci_bdf"]: item for item in mappings}

    rls_path = _find_latest(root, "rls.txt")
    rls_relative = _relative_source(root, rls_path) if rls_path else None
    rls_text = _read_text(root, rls_relative) if rls_relative else None
    kfd_nodes = parse_kfd_nodes(rls_text, rls_relative)
    node_metadata = {item["node_id"]: item for item in kfd_nodes}

    for target in target_metadata.get("target_gpus") or []:
        bdf = (target.get("pci_bdf") or "").lower()
        if not bdf:
            continue
        mapping = bdf_to_mapping.setdefault(
            bdf,
            {
                "node_id": target.get("node_id"),
                "amd_smi_id": target.get("amd_smi_id"),
                "pci_bdf": bdf,
                "source": "queue_doctor/target-gpus.json",
                "line": None,
            },
        )
        mapping.update(
            {
                "gpu_id": target.get("gpu_id"),
                "render_minor": target.get("render_minor"),
                "target": True,
            }
        )
    for mapping in mappings:
        node = node_metadata.get(mapping["node_id"])
        if node:
            mapping["gpu_id"] = node["gpu_id"]
        mapping.setdefault("target", False)

    hqds_path = _find_latest(root, "hqds.txt")
    hqds_relative = _relative_source(root, hqds_path) if hqds_path else None
    hqds_text = _read_text(root, hqds_relative) if hqds_relative else None
    hqd_records = parse_kfd_hqds(
        hqds_text, hqds_relative, node_to_bdf
    )
    active_hqds = [record for record in hqd_records if record.get("active")]

    queue_doctor = parse_queue_doctor(root, target_metadata)
    warnings.extend(queue_doctor["warnings"])

    cpc_paths, cpc_root, cpc_discovery_truncated = _discover_cpc_logs(root)
    if not cpc_paths:
        warnings.append(
            {
                "code": "CPC_LOGS_ABSENT",
                "message": "No bounded *umr_cpc*.txt captures were found.",
            }
        )
    if cpc_discovery_truncated:
        warnings.append(
            {
                "code": "CPC_LOG_LIMIT",
                "message": (
                    "CPC discovery reached its file/log limit; only the "
                    "bounded prefix was parsed."
                ),
            }
        )
    if cpc_discovery_truncated:
        metadata = {
            "command_map": {},
            "minor_to_bdf": {},
            "bdf_to_asic": {},
            "unique_asic": None,
            "warnings": [],
        }
    else:
        metadata = parse_cpc.scan_bundle_metadata(cpc_root)
    warnings.extend(metadata.get("warnings") or [])
    cpc_logs = []
    for path in cpc_paths:
        try:
            if os.path.getsize(path) > MAX_CPC_LOG_BYTES:
                warnings.append(
                    {
                        "code": "CPC_FILE_TOO_LARGE",
                        "message": "A CPC file exceeded the bounded parser limit.",
                        "source": _relative_source(root, path),
                    }
                )
                continue
            cpc_logs.append(parse_cpc.parse_log(path, metadata=metadata))
        except (IOError, OSError, RuntimeError) as error:
            warnings.append(
                {
                    "code": "CPC_PARSE_FAILED",
                    "message": "A CPC capture could not be parsed: %s" % error,
                    "source": _relative_source(root, path),
                }
            )

    qd_candidates = [
        item for item in queue_doctor["queues"] if item.get("mqd_base") is not None
    ]
    rows = []
    global_busy_contexts = 0
    for log in cpc_logs:
        context = log["context"]
        source = _relative_source(root, log["source"]["path"])
        busy_registers = {}
        for register in log["global_sections"].get("global_registers", []):
            if "BUSY" in register["name"] or "STALL" in register["name"]:
                busy_registers[register["name"]] = register["value"]
        if any(value for value in busy_registers.values()):
            global_busy_contexts += 1
        for queue in log["queues"]:
            derived = queue["derived"]
            decoded = derived.get("decoded") or {}
            pq = decoded.get("pq_control")
            status = classify_cpc_queue(queue)
            row = {
                "filename_gpu_index": context.get("filename_gpu_index"),
                "umr_instance": context.get("umr_instance"),
                "pci_bdf": context.get("pci_bdf"),
                "xcc": context.get("xcc"),
                "xcc_source": context.get("xcc_source"),
                "label": queue["identity"]["label"],
                "source": source,
                "source_line": queue["identity"].get("line"),
                "mqd_base": derived.get("mqd_base"),
                "mqd_base_hex": derived.get("mqd_base_hex"),
                "pq_base": derived.get("pq_ring_base"),
                "pq_base_hex": derived.get("pq_ring_base_hex"),
                "rptr": derived.get("raw_pointers", {}).get("rptr"),
                "wptr": derived.get("raw_pointers", {}).get("wptr"),
                "capacity": (derived.get("queue_size") or {}).get("slots_64b"),
                "capacity_bytes": (derived.get("queue_size") or {}).get("bytes"),
                "vmid": derived.get("vmid"),
                "pq_empty": pq.get("pq_empty") if pq else None,
                "queue_idle": (decoded.get("status") or {}).get("queue_idle"),
                "active": (derived.get("active") or {}).get("is_active"),
                "doorbell": decoded.get("doorbell_control"),
                "status": status,
                "warnings": list(queue.get("warnings") or []),
                "global_busy_registers": busy_registers,
                "snapshot_caveat": derived.get("snapshot_caveat"),
                "cpc_pending": (
                    (derived.get("aql") or {}).get("pending")
                    if derived.get("aql")
                    else None
                ),
            }
            qd_join = exact_mqd_join(row, qd_candidates)
            hqd_join = exact_mqd_join(row, active_hqds)
            row["queue_doctor_join"] = qd_join
            row["kfd_hqd_join"] = hqd_join
            if qd_join["status"] == "exact":
                matched = qd_join["matches"][0]
                row["queue_doctor"] = matched
                if matched.get("stall_candidate"):
                    row["status"] = "stall-candidate"
            if qd_join["status"] == "exact" or hqd_join["status"] == "exact":
                row["join_status"] = "exact"
                row["join_confidence"] = "high"
            elif (
                qd_join["status"] == "ambiguous"
                or hqd_join["status"] == "ambiguous"
            ):
                row["join_status"] = "ambiguous"
                row["join_confidence"] = "low"
            else:
                row["join_status"] = "unmatched"
                row["join_confidence"] = "none"
            rows.append(row)

    gpus = {}
    for bdf, mapping in bdf_to_mapping.items():
        gpus[bdf] = {
            "pci_bdf": bdf,
            "amd_smi_id": mapping.get("amd_smi_id"),
            "node_id": mapping.get("node_id"),
            "gpu_id": mapping.get("gpu_id"),
            "render_node": (
                "renderD%d" % mapping["render_minor"]
                if mapping.get("render_minor") is not None
                else None
            ),
            "target": mapping.get("target", False),
            "umr_instances": set(),
            "filename_gpu_indices": set(),
            "xccs": set(),
            "source": mapping.get("source"),
        }
    for row in rows:
        bdf = row.get("pci_bdf")
        if not bdf:
            continue
        gpu = gpus.setdefault(
            bdf,
            {
                "pci_bdf": bdf,
                "amd_smi_id": None,
                "node_id": None,
                "gpu_id": None,
                "render_node": None,
                "target": False,
                "umr_instances": set(),
                "filename_gpu_indices": set(),
                "xccs": set(),
                "source": row.get("source"),
            },
        )
        if row.get("umr_instance") is not None:
            gpu["umr_instances"].add(row["umr_instance"])
        if row.get("filename_gpu_index") is not None:
            gpu["filename_gpu_indices"].add(row["filename_gpu_index"])
        if row.get("xcc") is not None:
            gpu["xccs"].add(row["xcc"])
    for gpu in gpus.values():
        gpu["umr_instances"] = sorted(gpu["umr_instances"])
        gpu["filename_gpu_indices"] = sorted(gpu["filename_gpu_indices"])
        gpu["xccs"] = sorted(gpu["xccs"])

    root_summary = _read_text(root, "summary.txt")
    collector_statuses, completed_at = _parse_collector_summary(root_summary)
    system_info = _read_text(root, "system_info.txt")
    bundle_time = completed_at
    collected_at = None
    hostname = None
    kernel = None
    if system_info:
        for line in system_info.splitlines()[:20]:
            if line.startswith("collected_at="):
                collected_at = line.split("=", 1)[1]
            elif line.startswith("hostname="):
                hostname = line.split("=", 1)[1]
            elif line.startswith("kernel="):
                kernel = line.split("=", 1)[1]

    wave_summaries = []
    wave_dir = os.path.join(root, "waves")
    if os.path.isdir(wave_dir):
        wave_summaries = sorted(
            filename
            for filename in os.listdir(wave_dir)
            if filename.endswith("_summary.txt")
        )
    wave_detail = "No wave summary"
    wave_source = None
    if wave_summaries:
        wave_source = "waves/" + wave_summaries[-1]
        wave_text = _read_text(root, wave_source)
        values = {}
        for line in (wave_text or "").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        wave_detail = "%s/%s captures, %s samples" % (
            values.get("successful_captures", "?"),
            values.get("gpu_xcc_captures", "?"),
            values.get("samples", "?"),
        )

    source_panels = [
        _source_panel(
            "CPC",
            "available" if cpc_logs else "absent",
            "%d captures, %d active queues, %d busy/stall contexts"
            % (len(cpc_logs), len(rows), global_busy_contexts),
            "cpc/" if cpc_logs else None,
        ),
        _source_panel(
            "Queue Doctor",
            "available" if queue_doctor["queues"] else "absent",
            "%d queues across %d snapshots; pending=%d"
            % (
                len(queue_doctor["queues"]),
                queue_doctor["snapshot_count"],
                queue_doctor["pending_total"],
            ),
            queue_doctor.get("source"),
        ),
        _source_panel(
            "KFD snapshots",
            "available" if hqds_relative else "absent",
            "%d active HQDs parsed from latest bounded snapshot" % len(active_hqds),
            hqds_relative,
        ),
        _source_panel(
            "Waves",
            "available" if wave_source else "absent",
            wave_detail,
            wave_source,
        ),
        _source_panel(
            "SDMA",
            (collector_statuses.get("sdma_registers") or {}).get(
                "status", "available" if sdma_text else "absent"
            ).lower(),
            "Collector status; mapping metadata remains usable"
            if sdma_text
            else "No SDMA artifact",
            "sdma_registers.txt" if sdma_text else None,
        ),
        _source_panel(
            "System",
            "available" if system_info else "absent",
            "Kernel %s" % (kernel or "unknown"),
            "system_info.txt" if system_info else None,
        ),
        _source_panel(
            "AQL GDB",
            "available"
            if os.path.isfile(os.path.join(root, "aql_queue_dump", "aql_packet_decode_gdb.txt"))
            else "absent",
            "Decoded process AQL/SDMA queue snapshot"
            if os.path.isfile(os.path.join(root, "aql_queue_dump", "aql_packet_decode_gdb.txt"))
            else "Optional source absent",
            "aql_queue_dump/aql_packet_decode_gdb.txt"
            if os.path.isfile(os.path.join(root, "aql_queue_dump", "aql_packet_decode_gdb.txt"))
            else None,
        ),
    ]

    source_mtimes = []
    for row in rows:
        if row.get("source"):
            path = _bounded_file(root, row["source"], MAX_CPC_LOG_BYTES)
            if path:
                source_mtimes.append(os.path.getmtime(path))
    freshness = None
    if source_mtimes:
        freshness = {
            "oldest": datetime.datetime.fromtimestamp(min(source_mtimes)).isoformat(),
            "newest": datetime.datetime.fromtimestamp(max(source_mtimes)).isoformat(),
        }

    statuses = collections.Counter(row["status"] for row in rows)
    queue_warning_count = sum(len(row["warnings"]) for row in rows)
    warnings.append(
        {
            "code": "NON_ATOMIC_CAPTURE",
            "message": (
                "CPC, KFD, Queue Doctor, waves, and SDMA were captured at "
                "different times. A single CPC snapshot is never labeled a confirmed hang."
            ),
        }
    )
    target_pid = target_metadata.get("pid")
    if target_pid is None:
        target_pid = (_read_json(root, "queue_doctor/process-selection.json") or {}).get(
            "pid"
        )
    return {
        "bundle": {
            "identity": os.path.basename(root),
            "collected_at": collected_at,
            "completed_at": bundle_time,
            "hostname": hostname,
            "target_pid": target_pid,
            "analyzed_at": datetime.datetime.now().astimezone().isoformat(),
            "freshness": freshness,
        },
        "overview": {
            "gpu_count": len(gpus),
            "queue_count": len(rows),
            "active_count": sum(1 for row in rows if row["active"]),
            "nonempty_count": statuses["non-empty"] + statuses["stall-candidate"],
            "empty_count": statuses["empty-active"],
            "unknown_count": statuses["unknown"] + statuses["inactive-stale"],
            "stall_candidate_count": statuses["stall-candidate"],
            "pending_total": queue_doctor["pending_total"],
            "warning_count": len(warnings) + queue_warning_count,
            "cpc_file_count": len(cpc_logs),
        },
        "gpus": sorted(
            gpus.values(),
            key=lambda item: (
                item["amd_smi_id"] is None,
                item["amd_smi_id"] if item["amd_smi_id"] is not None else 999,
                item["pci_bdf"],
            ),
        ),
        "queues": rows,
        "queue_doctor": queue_doctor,
        "warnings": warnings,
        "source_panels": source_panels,
        "global_busy_contexts": global_busy_contexts,
    }


def analyze_bundle(root, force=False):
    """Analyze and cache a bundle by bounded source mtimes and sizes."""
    root = os.path.realpath(os.path.abspath(root))
    if not is_gpu_debug_bundle(root):
        raise ValueError("served root is not a recognized GPU debug bundle")
    signature = _signature(root)
    with _CACHE_LOCK:
        cached = _CACHE.get(root)
        if not force and cached and cached["signature"] == signature:
            return cached["model"]
    model = _analyze_uncached(root)
    with _CACHE_LOCK:
        _CACHE[root] = {"signature": signature, "model": model}
    return model


def _escape(value, fallback="\u2014"):
    if value is None or value == "":
        return fallback
    return html.escape(str(value))


def _link(relative, label, line=None, class_name="source-link"):
    url = source_url(relative, line)
    if url is None:
        return _escape(label)
    return '<a class="{}" href="{}">{}</a>'.format(
        html.escape(class_name, quote=True),
        html.escape(url, quote=True),
        html.escape(str(label)),
    )


def _format_flags(row):
    values = [
        "ACTIVE=%s" % ("1" if row.get("active") else "0"),
        "PQ_EMPTY=%s"
        % (
            "?"
            if row.get("pq_empty") is None
            else ("1" if row["pq_empty"] else "0")
        ),
        "QUEUE_IDLE=%s"
        % (
            "?"
            if row.get("queue_idle") is None
            else ("1" if row["queue_idle"] else "0")
        ),
    ]
    doorbell = row.get("doorbell")
    if doorbell:
        values.append(
            "DB_ENABLE=%s" % ("1" if doorbell.get("doorbell_enable") else "0")
        )
        values.append(
            "DB_HIT=%s" % ("1" if doorbell.get("doorbell_hit") else "0")
        )
    return " ".join(values)


def _render_queue_row(row, gpu):
    status = row["status"]
    qd_join = row["queue_doctor_join"]
    hqd_join = row["kfd_hqd_join"]
    qd = row.get("queue_doctor")
    evidence = [
        "<li>CPC source: {}</li>".format(
            _link(row["source"], row["source"], row.get("source_line"))
        ),
        "<li>Queue Doctor exact MQD join: <strong>{}</strong> ({})</li>".format(
            _escape(qd_join["status"]), _escape(qd_join["confidence"])
        ),
        "<li>KFD active-HQD exact MQD join: <strong>{}</strong> ({})</li>".format(
            _escape(hqd_join["status"]), _escape(hqd_join["confidence"])
        ),
    ]
    if qd:
        packet_types = ", ".join(
            "{}={}".format(key, value)
            for key, value in sorted((qd.get("packet_types") or {}).items())
        )
        evidence.append(
            "<li>Queue Doctor queue {}: state={}, snapshots={}, pending={}, "
            "packet types={}</li>".format(
                qd["queue_id"],
                _escape(qd.get("state")),
                len(qd.get("snapshots") or []),
                _escape(qd.get("pending_count")),
                _escape(packet_types or "none"),
            )
        )
        if qd.get("pending_source"):
            evidence.append(
                "<li>{}</li>".format(
                    _link(
                        qd["pending_source"],
                        "Open Queue Doctor pending packet log",
                    )
                )
            )
    if row.get("doorbell"):
        evidence.append(
            "<li>Doorbell bits are sampled configuration/state only; "
            "BIF_DROP is not evidence of an observed lost doorbell.</li>"
        )
    evidence.append(
        "<li>{}</li>".format(
            _escape(
                row.get("snapshot_caveat")
                or "CPC is a sequential, non-atomic snapshot."
            )
        )
    )
    search_text = " ".join(
        str(value)
        for value in (
            row.get("label"),
            row.get("mqd_base_hex"),
            row.get("pq_base_hex"),
            row.get("pci_bdf"),
            row.get("umr_instance"),
            row.get("filename_gpu_index"),
        )
        if value is not None
    ).lower()
    gpu_filter = (
        str(gpu.get("amd_smi_id"))
        if gpu.get("amd_smi_id") is not None
        else gpu.get("pci_bdf")
    )
    return """<tr class="queue-row" data-gpu="{gpu_filter}" data-xcc="{xcc}"
      data-status="{status}" data-search="{search}">
      <td><strong>{label}</strong><small>capture filename gpu{filename_gpu}</small></td>
      <td><span class="status status-{status_class}">{status_label}</span></td>
      <td><code>{mqd}</code><small>{join_status} join</small></td>
      <td><code>{pq}</code><small>{capacity} slots / {capacity_bytes}</small></td>
      <td><code>{rptr}</code> / <code>{wptr}</code></td>
      <td>VMID {vmid}<small>{flags}</small></td>
      <td><details><summary>Evidence</summary><ul>{evidence}</ul></details></td>
    </tr>""".format(
        gpu_filter=html.escape(str(gpu_filter), quote=True),
        xcc=html.escape(str(row.get("xcc", "")), quote=True),
        status=html.escape(status, quote=True),
        search=html.escape(search_text, quote=True),
        label=_escape(row.get("label")),
        filename_gpu=_escape(row.get("filename_gpu_index")),
        status_class=html.escape(status.replace("_", "-"), quote=True),
        status_label=_escape(status.replace("-", " ")),
        mqd=_escape(row.get("mqd_base_hex")),
        join_status=_escape(row.get("join_status")),
        pq=_escape(row.get("pq_base_hex")),
        capacity=_escape(row.get("capacity")),
        capacity_bytes=_escape(
            ("%d bytes" % row["capacity_bytes"])
            if row.get("capacity_bytes") is not None
            else None
        ),
        rptr=_escape(_hex(row.get("rptr"))),
        wptr=_escape(_hex(row.get("wptr"))),
        vmid=_escape(row.get("vmid")),
        flags=_escape(_format_flags(row)),
        evidence="".join(evidence),
    )


def render_dashboard(model, release_footer_html):
    """Render a self-contained dashboard without embedding bundle data in JS."""
    overview = model["overview"]
    gpu_by_bdf = {gpu["pci_bdf"]: gpu for gpu in model["gpus"]}
    queue_groups = collections.defaultdict(lambda: collections.defaultdict(list))
    for row in model["queues"]:
        queue_groups[row.get("pci_bdf")][row.get("xcc")].append(row)

    gpu_sections = []
    for gpu in model["gpus"]:
        xcc_sections = []
        grouped = queue_groups.get(gpu["pci_bdf"], {})
        for xcc in sorted(grouped, key=lambda value: (value is None, value or 0)):
            rows = "".join(_render_queue_row(row, gpu) for row in grouped[xcc])
            xcc_sections.append(
                """<section class="xcc-section"><h4>XCC/partition {xcc}
                <span>{count} CPC active queues</span></h4>
                <div class="table-wrap"><table><thead><tr>
                <th>Queue</th><th>Status</th><th>MQD / join</th><th>PQ / capacity</th>
                <th>Raw RPTR / WPTR</th><th>State</th><th>Details</th>
                </tr></thead><tbody>{rows}</tbody></table></div></section>""".format(
                    xcc=_escape(xcc),
                    count=len(grouped[xcc]),
                    rows=rows,
                )
            )
        if not xcc_sections:
            xcc_sections.append(
                '<p class="empty-state">No CPC queue captures were mapped to this GPU.</p>'
            )
        gpu_sections.append(
            """<article class="gpu-card" data-gpu-card="{gpu_filter}">
              <header><div><p class="eyebrow">Physical GPU</p>
              <h3>AMD-SMI GPU {amd} <code>{bdf}</code>{target}</h3></div>
              <div class="gpu-meta">
                <span>KFD gpu_id <strong>{gpu_id}</strong></span>
                <span>node <strong>{node}</strong></span>
                <span>{render}</span>
                <span>UMR instance(s) <strong>{umr}</strong></span>
                <span>capture filename context(s) <strong>gpu{file_gpu}</strong></span>
              </div></header>{xcc_sections}</article>""".format(
                gpu_filter=html.escape(
                    str(
                        gpu["amd_smi_id"]
                        if gpu["amd_smi_id"] is not None
                        else gpu["pci_bdf"]
                    ),
                    quote=True,
                ),
                amd=_escape(gpu.get("amd_smi_id")),
                bdf=_escape(gpu.get("pci_bdf")),
                target=(
                    ' <span class="target-badge">target process</span>'
                    if gpu.get("target")
                    else ""
                ),
                gpu_id=_escape(gpu.get("gpu_id")),
                node=_escape(gpu.get("node_id")),
                render=_escape(gpu.get("render_node"), "render unknown"),
                umr=_escape(", ".join(str(x) for x in gpu["umr_instances"])),
                file_gpu=_escape(
                    ", gpu".join(str(x) for x in gpu["filename_gpu_indices"])
                ),
                xcc_sections="".join(xcc_sections),
            )
        )

    inventory_rows = []
    for gpu in model["gpus"]:
        inventory_rows.append(
            "<tr><td>{amd}</td><td><code>{bdf}</code></td><td>{node}</td>"
            "<td>{gpu_id}</td><td>{render}</td><td>{umr}</td><td>gpu{file_gpu}</td></tr>".format(
                amd=_escape(gpu.get("amd_smi_id")),
                bdf=_escape(gpu.get("pci_bdf")),
                node=_escape(gpu.get("node_id")),
                gpu_id=_escape(gpu.get("gpu_id")),
                render=_escape(gpu.get("render_node")),
                umr=_escape(", ".join(str(x) for x in gpu["umr_instances"])),
                file_gpu=_escape(
                    ", gpu".join(str(x) for x in gpu["filename_gpu_indices"])
                ),
            )
        )

    source_cards = []
    for panel in model["source_panels"]:
        source_cards.append(
            """<article class="source-card"><div><strong>{name}</strong>
              <span class="source-status source-{status}">{status}</span></div>
              <p>{detail}</p>{link}</article>""".format(
                name=_escape(panel["name"]),
                status=html.escape(panel["status"].lower(), quote=True),
                detail=_escape(panel["detail"]),
                link=(
                    _link(panel["source"], "Open source")
                    if panel.get("source")
                    else '<span class="muted">Optional source unavailable</span>'
                ),
            )
        )

    warning_items = "".join(
        "<li><strong>{}</strong> {}</li>".format(
            _escape(warning.get("code")), _escape(warning.get("message"))
        )
        for warning in model["warnings"]
    )
    gpu_options = "".join(
        '<option value="{value}">GPU {label} · {bdf}</option>'.format(
            value=html.escape(
                str(
                    gpu["amd_smi_id"]
                    if gpu["amd_smi_id"] is not None
                    else gpu["pci_bdf"]
                ),
                quote=True,
            ),
            label=_escape(gpu.get("amd_smi_id")),
            bdf=_escape(gpu.get("pci_bdf")),
        )
        for gpu in model["gpus"]
    )
    xcc_values = sorted(
        {row["xcc"] for row in model["queues"] if row.get("xcc") is not None}
    )
    xcc_options = "".join(
        '<option value="{0}">XCC {0}</option>'.format(value) for value in xcc_values
    )
    bundle = model["bundle"]
    freshness = bundle.get("freshness") or {}
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>GPU Queue Analysis · {bundle_name}</title>
<script>(()=>{{const supported=['light','dark','amber'];let theme=matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';try{{const saved=localStorage.getItem('gpu-bundle-theme');if(supported.includes(saved))theme=saved;}}catch(_){{}}document.documentElement.dataset.theme=theme;}})();</script>
<style>
:root{{color-scheme:light;--bg:#f3f6fb;--surface:#fff;--surface2:#f8faff;--text:#1d293d;--muted:#65738a;--line:#dfe5ef;--accent:#315ecb;--accent2:#244ba9;--accentText:#fff;--warn:#9a6700;--danger:#b42318;--success:#08783f;--focus:rgba(49,94,203,.38);--shadow:0 15px 40px rgba(23,42,78,.10)}}
:root[data-theme="dark"]{{color-scheme:dark;--bg:#0f1520;--surface:#171f2d;--surface2:#1d2737;--text:#edf2fb;--muted:#aab6c8;--line:#303c50;--accent:#8eafff;--accent2:#b0c6ff;--accentText:#101827;--warn:#ffd166;--danger:#ff8a80;--success:#65d89b;--focus:rgba(142,175,255,.42);--shadow:0 18px 50px rgba(0,0,0,.30)}}
:root[data-theme="amber"]{{color-scheme:dark;--bg:#15120c;--surface:#211b11;--surface2:#2c2314;--text:#fff4d0;--muted:#d2bb7a;--line:#5d4822;--accent:#f4c95d;--accent2:#ffdc7a;--accentText:#211600;--warn:#ffd76a;--danger:#ff9587;--success:#a7df8b;--focus:rgba(244,201,93,.48);--shadow:0 18px 50px rgba(5,3,0,.48)}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}a{{color:var(--accent)}}a:focus-visible,input:focus-visible,select:focus-visible{{outline:3px solid var(--focus);outline-offset:2px}}code{{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}}.shell{{width:min(1580px,calc(100% - 28px));min-height:100vh;margin:auto;padding:28px 0;display:flex;flex-direction:column}}.topbar{{display:flex;justify-content:space-between;flex-wrap:wrap;gap:18px;margin-bottom:18px}}.eyebrow{{margin:0 0 3px;color:var(--accent);font-size:11px;font-weight:800;letter-spacing:.12em;text-transform:uppercase}}h1{{margin:0;font-size:clamp(27px,4vw,42px);letter-spacing:-.04em}}h2{{margin:28px 0 12px;font-size:22px}}h3{{margin:2px 0;font-size:20px}}h4{{display:flex;justify-content:space-between;margin:18px 0 8px}}h4 span{{color:var(--muted);font-size:12px;font-weight:500}}.subtitle,.muted{{color:var(--muted)}}.header-actions,.filters{{display:flex;align-items:center;flex-wrap:wrap;gap:8px}}.button,.theme-control{{border:1px solid var(--line);border-radius:9px;background:var(--surface);color:var(--text);padding:8px 11px;text-decoration:none;font-weight:700}}.button.primary{{border-color:var(--accent);background:var(--accent);color:var(--accentText)}}.theme-control{{display:flex;gap:7px;color:var(--muted)}}select,input{{border:1px solid var(--line);border-radius:7px;background:var(--surface2);color:var(--text);padding:7px 9px;font:inherit}}.identity{{padding:14px 16px;border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:10px;background:var(--surface)}}.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px;margin:14px 0}}.metric{{padding:14px;border:1px solid var(--line);border-radius:11px;background:var(--surface);box-shadow:var(--shadow)}}.metric strong{{display:block;font-size:26px}}.metric span{{color:var(--muted)}}.caveat{{padding:12px 14px;border:1px solid var(--line);border-radius:10px;background:var(--surface2);color:var(--muted)}}.caveat strong{{color:var(--warn)}}.table-wrap{{overflow:auto;border:1px solid var(--line);border-radius:10px}}table{{width:100%;border-collapse:collapse;background:var(--surface)}}th,td{{padding:9px 11px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}th{{position:sticky;top:0;background:var(--surface2);color:var(--muted);font-size:11px;text-transform:uppercase;z-index:1}}td small{{display:block;color:var(--muted);white-space:normal}}.inventory table{{min-width:760px}}.filters{{position:sticky;top:0;z-index:4;padding:10px;border:1px solid var(--line);border-radius:10px;background:var(--surface);box-shadow:var(--shadow)}}.filters input{{min-width:260px;flex:1}}.gpu-card{{margin:14px 0;padding:16px;border:1px solid var(--line);border-radius:13px;background:var(--surface);box-shadow:var(--shadow)}}.gpu-card>header{{display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px}}.gpu-meta{{display:flex;flex-wrap:wrap;gap:6px}}.gpu-meta span,.target-badge{{padding:3px 7px;border:1px solid var(--line);border-radius:999px;background:var(--surface2);font-size:12px}}.target-badge{{color:var(--accent);font-weight:700}}.queue-row[data-hidden="true"],.gpu-card[data-hidden="true"]{{display:none}}.status{{display:inline-block;padding:3px 7px;border:1px solid currentColor;border-radius:999px;font-size:11px;font-weight:800;white-space:nowrap}}.status-stall-candidate,.status-non-empty{{color:var(--warn)}}.status-empty-active{{color:var(--success)}}.status-unknown,.status-inactive-stale{{color:var(--muted)}}details{{min-width:125px}}details ul{{min-width:280px;padding-left:18px}}.sources{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px}}.source-card{{padding:12px;border:1px solid var(--line);border-radius:10px;background:var(--surface)}}.source-card>div{{display:flex;justify-content:space-between}}.source-card p{{color:var(--muted)}}.source-status{{font-size:11px;font-weight:800;text-transform:uppercase}}.source-fail{{color:var(--danger)}}.source-available,.source-pass{{color:var(--success)}}.warnings{{padding:12px 30px;border:1px solid var(--line);border-radius:10px;background:var(--surface2)}}.empty-state{{padding:20px;color:var(--muted);text-align:center}}.release-footer{{margin-top:auto;border-top:1px solid var(--line);padding:16px 4px 0;color:var(--muted);font-size:12px;text-align:center}}
@media(max-width:760px){{.shell{{width:min(100% - 12px,1580px);padding-top:14px}}.header-actions,.theme-control{{width:100%}}.theme-control select{{flex:1}}.filters{{position:static}}.filters>*{{width:100%}}.filters input{{min-width:0}}.gpu-card{{padding:10px}}h4{{align-items:flex-start;gap:8px}}th,td{{padding:7px 8px}}}}
</style></head><body><main class="shell">
<header class="topbar"><div><p class="eyebrow">GPU forensics</p><h1>Queue Analysis Dashboard</h1>
<p class="subtitle">{bundle_name}</p></div><div class="header-actions">
<a class="button" href="/">Directory</a><a class="button primary" href="/__dashboard__/?refresh=1">Refresh / re-analyze</a>
<label class="theme-control">Theme <select id="theme-select"><option value="light">Light</option><option value="dark">Dark</option><option value="amber">暗黄</option></select></label>
</div></header>
<section class="identity"><strong>Bundle identity:</strong> {bundle_name} · target PID {pid} · host {hostname}<br>
Collected {collected} · completed {completed} · analyzed {analyzed}<br>
<span class="muted">Source freshness (CPC mtimes): {oldest} → {newest}</span></section>
<section class="metrics">
<div class="metric"><strong>{gpu_count}</strong><span>physical GPUs</span></div>
<div class="metric"><strong>{active}</strong><span>CPC active queues</span></div>
<div class="metric"><strong>{nonempty}</strong><span>non-empty</span></div>
<div class="metric"><strong>{empty}</strong><span>empty active</span></div>
<div class="metric"><strong>{unknown}</strong><span>unknown / inactive</span></div>
<div class="metric"><strong>{stall}</strong><span>stall candidates</span></div>
<div class="metric"><strong>{pending}</strong><span>authoritative pending packets</span></div>
<div class="metric"><strong>{warnings}</strong><span>parse warnings</span></div>
</section>
<p class="caveat"><strong>Interpretation caveat:</strong> Sources are non-atomic and have different capture times. A non-empty CPC queue is neutral; only repeated same-context Queue Doctor pointer/state evidence is labeled a stall candidate, never a confirmed hang.</p>
<h2>Physical GPU mapping</h2><div class="table-wrap inventory"><table><thead><tr><th>AMD-SMI</th><th>PCI BDF</th><th>KFD node</th><th>gpu_id</th><th>Render</th><th>UMR instance</th><th>Filename context</th></tr></thead><tbody>{inventory}</tbody></table></div>
<h2>Queue analysis</h2>
<div class="filters"><input id="queue-search" type="search" placeholder="Search MQD, PQ, PCI, queue…"><select id="gpu-filter"><option value="">All GPUs</option>{gpu_options}</select><select id="xcc-filter"><option value="">All XCCs</option>{xcc_options}</select><select id="status-filter"><option value="">All statuses</option><option value="stall-candidate">Stall candidate</option><option value="non-empty">Non-empty</option><option value="empty-active">Empty active</option><option value="unknown">Unknown</option><option value="inactive-stale">Inactive/stale</option></select><span id="visible-count" class="muted"></span></div>
<div id="queue-groups">{gpu_sections}</div>
<h2>Source status</h2><section class="sources">{source_cards}</section>
<h2>Warnings and limits</h2><ul class="warnings">{warning_items}</ul>
{release_footer}
</main><script>
const themeSelect=document.getElementById('theme-select');themeSelect.value=['light','dark','amber'].includes(document.documentElement.dataset.theme)?document.documentElement.dataset.theme:'light';themeSelect.addEventListener('change',()=>{{document.documentElement.dataset.theme=themeSelect.value;try{{localStorage.setItem('gpu-bundle-theme',themeSelect.value)}}catch(_){{}}}});
const controls=['queue-search','gpu-filter','xcc-filter','status-filter'].map(id=>document.getElementById(id));function filterRows(){{const query=controls[0].value.toLowerCase().trim(),gpu=controls[1].value,xcc=controls[2].value,status=controls[3].value;let visible=0;document.querySelectorAll('.queue-row').forEach(row=>{{const show=(!query||row.dataset.search.includes(query))&&(!gpu||row.dataset.gpu===gpu)&&(!xcc||row.dataset.xcc===xcc)&&(!status||row.dataset.status===status);row.dataset.hidden=show?'false':'true';if(show)visible++;}});document.querySelectorAll('.gpu-card').forEach(card=>{{card.dataset.hidden=card.querySelector('.queue-row[data-hidden="false"]')?'false':'true';}});document.getElementById('visible-count').textContent=visible+' queues shown';}}controls.forEach(control=>control.addEventListener('input',filterRows));filterRows();
</script></body></html>""".format(
        bundle_name=_escape(bundle.get("identity")),
        pid=_escape(bundle.get("target_pid")),
        hostname=_escape(bundle.get("hostname")),
        collected=_escape(bundle.get("collected_at")),
        completed=_escape(bundle.get("completed_at")),
        analyzed=_escape(bundle.get("analyzed_at")),
        oldest=_escape(freshness.get("oldest")),
        newest=_escape(freshness.get("newest")),
        gpu_count=overview["gpu_count"],
        active=overview["active_count"],
        nonempty=overview["nonempty_count"],
        empty=overview["empty_count"],
        unknown=overview["unknown_count"],
        stall=overview["stall_candidate_count"],
        pending=overview["pending_total"],
        warnings=overview["warning_count"],
        inventory="".join(inventory_rows),
        gpu_options=gpu_options,
        xcc_options=xcc_options,
        gpu_sections="".join(gpu_sections),
        source_cards="".join(source_cards),
        warning_items=warning_items,
        release_footer=release_footer_html,
    )
