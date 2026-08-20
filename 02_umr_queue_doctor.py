#!/usr/bin/env python3
"""Discover, dump, and analyze one process's UMR user queues.

Author: Perry_Yuan@outlook.com
Log History:
    2026-08-03 - Added automatic process/GPU discovery and queue diagnostics.
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


TOPOLOGY_ROOT = Path("/sys/class/kfd/kfd/topology/nodes")
KFD_PROCESS_ROOT = Path("/sys/class/kfd/kfd/proc")
KFD_MQDS_PATH = Path("/sys/kernel/debug/kfd/mqds")
DEFAULT_OUTPUT_ROOT = Path(
    os.environ.get(
        "DEBUG_TOOLS_TMP_DIR",
        f"/tmp/debug-tools-{os.environ.get('SUDO_UID', os.getuid())}",
    )
)
QUEUE_TYPE_NAMES = {
    0: "gfx",
    1: "compute",
    2: "compute-pm4",
    3: "sdma",
}
IDLE_QUEUE_TEXT = "RPTR and WPTR are equal"
DECODE_ERROR_PATTERNS = (
    re.compile(r"\[ERROR\]", re.IGNORECASE),
    re.compile(r"\b(?:invalid|unknown|unhandled)\s+(?:packet|opcode)", re.IGNORECASE),
    re.compile(r"\b(?:failed|unable)\s+to\s+(?:read|decode)", re.IGNORECASE),
    re.compile(r"\bcannot\s+(?:read|decode)", re.IGNORECASE),
    re.compile(r"\bpacket\s+failed\s+to\s+size\s+correctly\b", re.IGNORECASE),
    re.compile(r"\bVM\s+fault\b", re.IGNORECASE),
)


class NodeInfo:
    def __init__(
        self,
        node_id,
        gpu_id,
        name,
        pci_bdf,
        domain,
        location_id,
        render_minor,
        gfx_target_version,
        amd_smi_id=None,
    ):
        self.node_id = node_id
        self.gpu_id = gpu_id
        self.name = name
        self.pci_bdf = pci_bdf
        self.domain = domain
        self.location_id = location_id
        self.render_minor = render_minor
        self.gfx_target_version = gfx_target_version
        self.amd_smi_id = amd_smi_id


class KfdQueueInfo:
    """Authoritative queue-to-GPU identity from KFD process sysfs."""

    def __init__(self, queue_id, gpu_id, queue_type=None, size=None):
        self.queue_id = queue_id
        self.gpu_id = gpu_id
        self.queue_type = queue_type
        self.size = size


class MqdQueueIdentity:
    """UMR's local MQD-list index annotated by KFD debugfs."""

    def __init__(
        self,
        queue_id,
        gpu_id,
        queue_type,
        mqd_gpu_address=None,
    ):
        self.queue_id = queue_id
        self.gpu_id = gpu_id
        self.queue_type = queue_type
        self.mqd_gpu_address = mqd_gpu_address

    def signature(self):
        return (
            self.queue_id,
            self.gpu_id,
            self.queue_type,
            self.mqd_gpu_address,
        )


class GpuInventory:
    def __init__(
        self,
        queues,
        target_nodes,
        selected_nodes,
        visible_nodes,
        amd_smi_gpu_bdfs,
    ):
        self.queues = queues
        self.target_nodes = target_nodes
        self.selected_nodes = selected_nodes
        self.visible_nodes = visible_nodes
        self.amd_smi_gpu_bdfs = amd_smi_gpu_bdfs


class QueueInfo:
    def __init__(
        self,
        queue_id,
        queue_type,
        queue_type_name,
        mqd_gpu_address,
        rptr,
        wptr,
        selected,
    ):
        self.queue_id = queue_id
        self.queue_type = queue_type
        self.queue_type_name = queue_type_name
        self.mqd_gpu_address = mqd_gpu_address
        self.rptr = rptr
        self.wptr = wptr
        self.selected = selected
        self.kfd_gpu_id = None
        self.mapping_source = None

    @property
    def pending(self):
        return self.rptr != self.wptr


class QueueState:
    def __init__(self, queue_id):
        self.queue_id = queue_id
        self.queue_type = None
        self.queue_type_name = None
        self.mqd_gpu_address = None
        self.hqd_base_address = None
        self.rb_buf_size_words = None
        self.hqd_rptr_address = None
        self.hqd_rptr_value = None
        self.wptr_poll_address = None
        self.wptr_poll_value = None
        self.hqd_active = None
        self.ring_size_bytes = None
        self.sdma_engine_id = None
        self.sdma_queue_id = None
        self.doorbell_id = None
        self.doorbell_address = None
        self.doorbell_value = None
        self.memory_read_dispatch_id = None
        self.memory_write_dispatch_id = None
        self.compute_current_logic_xcc_id = None
        self.pm4_target_xcc_in_xcp = None
        self.hardware_rptr = None
        self.hardware_wptr = None


class PacketDump:
    def __init__(
        self,
        queue_id,
        mode,
        path,
        command_status,
        idle,
        declared_words,
        decoded_packets,
        decoded_packet_words,
        packet_types,
        decoder_completed,
        timeout_seconds,
        timed_out,
        command,
        started_at=None,
        finished_at=None,
    ):
        self.queue_id = queue_id
        self.mode = mode
        self.path = path
        self.command_status = command_status
        self.idle = idle
        self.declared_words = declared_words
        self.decoded_packets = decoded_packets
        self.decoded_packet_words = decoded_packet_words
        self.packet_types = packet_types
        self.decoder_completed = decoder_completed
        self.command = list(command)
        self.timeout_seconds = timeout_seconds
        self.timed_out = timed_out
        self.started_at = started_at
        self.finished_at = finished_at


class Snapshot:
    def __init__(
        self,
        index,
        timestamp,
        list_path,
        state_path,
        queues=None,
        states=None,
        packet_dumps=None,
        sdma_register_dumps=None,
        full_ring_artifacts=None,
    ):
        self.index = index
        self.timestamp = timestamp
        self.list_path = list_path
        self.state_path = state_path
        self.queues = queues or []
        self.states = states or []
        self.packet_dumps = packet_dumps or []
        self.sdma_register_dumps = sdma_register_dumps or []
        self.full_ring_artifacts = full_ring_artifacts or []


class Finding:
    def __init__(self, severity, code, message, queue_id=None, snapshot=None):
        self.severity = severity
        self.code = code
        self.message = message
        self.queue_id = queue_id
        self.snapshot = snapshot


class CommandResult:
    def __init__(
        self,
        argv,
        returncode,
        output,
        timed_out=False,
        started_at=None,
        finished_at=None,
    ):
        self.argv = argv
        self.returncode = returncode
        self.output = output
        self.timed_out = timed_out
        self.started_at = started_at
        self.finished_at = finished_at


class BinaryCommandResult:
    def __init__(
        self,
        argv,
        returncode,
        data,
        stderr=b"",
        timed_out=False,
        started_at=None,
        finished_at=None,
    ):
        self.argv = argv
        self.returncode = returncode
        self.data = data
        self.stderr = stderr
        self.timed_out = timed_out
        self.started_at = started_at
        self.finished_at = finished_at


class FatalError(RuntimeError):
    pass


def record_to_dict(value):
    if isinstance(value, list):
        return [record_to_dict(item) for item in value]
    if isinstance(value, dict):
        return {key: record_to_dict(item) for key, item in value.items()}
    if hasattr(value, "__dict__"):
        return {
            key: record_to_dict(item)
            for key, item in vars(value).items()
        }
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Select a live process and one or more AMD-SMI GPU IDs, dump "
            "their UMR queue state/packets sequentially, and report packet or "
            "progress anomalies. Only GPUs selected from the PID's KFD queue "
            "inventory are printed and passed to UMR."
        )
    )
    parser.add_argument(
        "--pid",
        type=int,
        help=(
            "host process ID; when omitted, discover --process-name through "
            "amd-smi"
        ),
    )
    parser.add_argument(
        "--process-name",
        default="torch_server.bin",
        help=(
            "exact amd-smi process name used when --pid is omitted "
            "(default: torch_server.bin)"
        ),
    )
    gpu_group = parser.add_mutually_exclusive_group()
    gpu_group.add_argument(
        "--gpu-id",
        dest="gpu_ids",
        type=int,
        nargs="+",
        help=(
            "one or more GPU numbers from the first GPU column of amd-smi, "
            "e.g. --gpu-id 0 1 2"
        ),
    )
    gpu_group.add_argument(
        "--node-id",
        dest="node_ids",
        type=int,
        nargs="+",
        help=(
            "legacy KFD topology node IDs; prefer --gpu-id for new usage"
        ),
    )
    gpu_group.add_argument(
        "--all-gpus",
        action="store_true",
        help="automatically dump every GPU on which the PID owns a KFD queue",
    )
    parser.add_argument(
        "--umr-bin",
        type=Path,
        help="UMR executable; otherwise search known locations and PATH",
    )
    parser.add_argument(
        "--vm-partition",
        type=int,
        default=0,
        help="UMR VM partition (default: 0)",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=2,
        help="number of live snapshots used for progress detection (default: 2)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=20.0,
        help="seconds between snapshots (default: 20.0)",
    )
    parser.add_argument(
        "--full-ring",
        action="store_true",
        help=(
            "compatibility option; full physical-ring capture is enabled by "
            "default and writes a separate full-ring/ dump set"
        ),
    )
    parser.add_argument(
        "--no-full-ring",
        action="store_true",
        help="opt out of default full physical-ring VM capture (cost-saving)",
    )
    raw_retention_group = parser.add_mutually_exclusive_group()
    raw_retention_group.add_argument(
        "--keep-full-ring-raw",
        dest="keep_full_ring_raw",
        action="store_true",
        help=(
            "compatibility option; successful full-ring VM-read .bin captures "
            "are retained by default"
        ),
    )
    raw_retention_group.add_argument(
        "--discard-full-ring-raw",
        dest="keep_full_ring_raw",
        action="store_false",
        help=(
            "delete full-ring VM-read .bin captures after successful decode; "
            "failed or incomplete decodes always retain available raw data"
        ),
    )
    parser.set_defaults(keep_full_ring_raw=True)
    parser.add_argument(
        "--queue",
        dest="queue_ids",
        type=int,
        action="append",
        help=(
            "only dump this UMR-local queue ID from --list-uq; this is not "
            "the global KFD sysfs queue directory ID; may be repeated"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help=(
            "artifact directory "
            f"(default: timestamped directory under {DEFAULT_OUTPUT_ROOT})"
        ),
    )
    parser.add_argument(
        "--command-timeout",
        type=float,
        default=60.0,
        help="timeout in seconds for each UMR command (default: 60)",
    )
    parser.add_argument(
        "--no-sudo",
        action="store_true",
        help="run UMR directly even when not root",
    )
    parser.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="return status 2 when heuristic warnings are found",
    )
    parser.add_argument(
        "--inventory-only",
        action="store_true",
        help=(
            "print the authoritative PID target-GPU inventory and exit "
            "without resolving UMR or reading GPU state"
        ),
    )
    args = parser.parse_args()

    if args.pid is not None and args.pid <= 0:
        parser.error("--pid must be positive")
    if not args.process_name:
        parser.error("--process-name must not be empty")
    if args.gpu_ids and any(gpu_id < 0 for gpu_id in args.gpu_ids):
        parser.error("--gpu-id values must be non-negative")
    if args.node_ids and any(node_id < 0 for node_id in args.node_ids):
        parser.error("--node-id values must be non-negative")
    if args.samples <= 0:
        parser.error("--samples must be positive")
    if args.interval < 0:
        parser.error("--interval must be non-negative")
    if args.command_timeout <= 0:
        parser.error("--command-timeout must be positive")
    if not args.gpu_ids and not args.node_ids and not args.all_gpus:
        args.all_gpus = True
    return args


def parse_key_value_file(path: Path) -> Dict[str, int]:
    values = {}  # type: Dict[str, int]
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) >= 2:
            try:
                values[fields[0]] = int(fields[1], 0)
            except ValueError:
                continue
    return values


def resolve_node(node_id: int) -> NodeInfo:
    node_path = TOPOLOGY_ROOT / str(node_id)
    properties_path = node_path / "properties"
    gpu_id_path = node_path / "gpu_id"
    if not properties_path.is_file() or not gpu_id_path.is_file():
        raise FatalError(f"KFD topology node {node_id} does not exist")

    properties = parse_key_value_file(properties_path)
    try:
        gpu_id = int(gpu_id_path.read_text(encoding="utf-8").strip(), 0)
    except ValueError as exc:
        raise FatalError(f"invalid gpu_id for topology node {node_id}") from exc
    if gpu_id == 0 or properties.get("simd_count", 0) == 0:
        raise FatalError(f"KFD topology node {node_id} is not a GPU node")

    location_id = properties.get("location_id")
    domain = properties.get("domain", 0)
    if location_id is None:
        raise FatalError(f"topology node {node_id} has no location_id")

    bus = (location_id >> 8) & 0xFF
    device = (location_id >> 3) & 0x1F
    function = location_id & 0x7
    pci_bdf = f"{domain:04x}:{bus:02x}:{device:02x}.{function:x}"
    if not (Path("/sys/bus/pci/devices") / pci_bdf).exists():
        raise FatalError(
            f"topology node {node_id} maps to missing PCI device {pci_bdf}"
        )

    name_path = node_path / "name"
    name = (
        name_path.read_text(encoding="utf-8").strip()
        if name_path.is_file()
        else "unknown"
    )
    return NodeInfo(
        node_id=node_id,
        gpu_id=gpu_id,
        name=name,
        pci_bdf=pci_bdf,
        domain=domain,
        location_id=location_id,
        render_minor=properties.get("drm_render_minor", -1),
        gfx_target_version=properties.get("gfx_target_version", 0),
    )


def resolve_amd_smi() -> Path:
    candidates = []  # type: List[Path]
    path_amd_smi = shutil.which("amd-smi")
    if path_amd_smi:
        candidates.append(Path(path_amd_smi))
    candidates.extend(
        [
            Path("/usr/bin/amd-smi"),
            Path("/usr/local/bin/amd-smi"),
            Path("/opt/rocm/bin/amd-smi"),
        ]
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise FatalError("amd-smi executable was not found")


def run_amd_smi(timeout: float, purpose: str) -> str:
    amd_smi = resolve_amd_smi()
    try:
        completed = subprocess.run(
            [str(amd_smi)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise FatalError(f"amd-smi timed out while {purpose}")
    if completed.returncode != 0:
        raise FatalError(
            f"amd-smi failed with status {completed.returncode} while {purpose}"
        )
    return completed.stdout


def discover_process_from_amd_smi(
    process_name: str, timeout: float
) -> Tuple[int, List[int], str]:
    output = run_amd_smi(timeout, f"discovering process {process_name!r}")
    process_re = re.compile(r"^\|\s*(\d+)\s+(\d+)\s+(\S+)\s+")
    gpu_ids_by_pid = {}  # type: Dict[int, set]
    for line in output.splitlines():
        match = process_re.match(line)
        if not match or match.group(3) != process_name:
            continue
        gpu_id = int(match.group(1))
        pid = int(match.group(2))
        gpu_ids_by_pid.setdefault(pid, set()).add(gpu_id)

    if not gpu_ids_by_pid:
        raise FatalError(
            f"amd-smi found no process named {process_name!r}; "
            "pass --pid to select a process explicitly"
        )
    if len(gpu_ids_by_pid) > 1:
        candidates = "; ".join(
            f"PID {pid} on AMD-SMI GPU(s) "
            + ",".join(str(gpu_id) for gpu_id in sorted(gpu_ids))
            for pid, gpu_ids in sorted(gpu_ids_by_pid.items())
        )
        raise FatalError(
            f"amd-smi found multiple processes named {process_name!r}: "
            f"{candidates}; pass --pid to disambiguate"
        )

    pid = next(iter(gpu_ids_by_pid))
    return pid, sorted(gpu_ids_by_pid[pid]), output


def read_amd_smi_gpu_bdfs(timeout: float) -> Dict[int, str]:
    output = run_amd_smi(timeout, "resolving GPU IDs")

    bdf_re = re.compile(
        r"^\|\s*([0-9a-fA-F]{4}:[0-9a-fA-F]{2}:"
        r"[0-9a-fA-F]{2}\.[0-7])\s+"
    )
    gpu_id_re = re.compile(r"^\|\s*(\d+)\s+\d+\s+\S+\s+")
    gpu_bdfs = {}  # type: Dict[int, str]
    pending_bdf = None  # type: Optional[str]
    for line in output.splitlines():
        bdf_match = bdf_re.match(line)
        if bdf_match:
            pending_bdf = bdf_match.group(1).lower()
            continue
        if pending_bdf is None:
            continue
        gpu_match = gpu_id_re.match(line)
        if not gpu_match:
            continue
        gpu_id = int(gpu_match.group(1))
        if gpu_id in gpu_bdfs and gpu_bdfs[gpu_id] != pending_bdf:
            raise FatalError(f"amd-smi reported duplicate GPU ID {gpu_id}")
        gpu_bdfs[gpu_id] = pending_bdf
        pending_bdf = None

    if not gpu_bdfs:
        raise FatalError(
            "could not parse GPU IDs and PCI BDFs from the amd-smi output"
        )
    return gpu_bdfs


def resolve_umr(explicit: Optional[Path]) -> Path:
    candidates = []  # type: List[Path]
    if explicit is not None:
        if explicit.is_dir():
            candidates.extend(
                [
                    explicit / "build/src/app/umr",
                    explicit / "src/app/umr",
                ]
            )
        else:
            candidates.append(explicit)
    candidates.extend(
        [
            Path("/home/weiyuan2/umr/build/src/app/umr"),
            Path(
                "/home/weiyuan2/test-case/ali-models/umr/"
                "build/src/app/umr"
            ),
            Path("/home/admin/umr/src/app/umr"),
            Path("/home/admin/umr/build/src/app/umr"),
            Path("/usr/local/bin/umr"),
            Path("/home/weiyuan2/umr-list-uq/build/src/app/umr"),
            Path("/home/weiyuan2/umr-list-uq/umr/build/src/app/umr"),
        ]
    )
    path_umr = shutil.which("umr")
    if path_umr:
        candidates.append(Path(path_umr))

    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise FatalError("UMR was not found; pass --umr-bin /path/to/umr")


def _read_optional_int(path: Path) -> Optional[int]:
    if not path.is_file():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip(), 0)
    except (OSError, ValueError):
        return None


def read_process_queues(pid: int) -> List[KfdQueueInfo]:
    """Read one stable snapshot of KFD's authoritative PID queue inventory."""

    queue_root = KFD_PROCESS_ROOT / str(pid) / "queues"
    if not queue_root.is_dir():
        raise FatalError(
            f"KFD per-process queue metadata is unavailable: {queue_root}"
        )

    last_error = "queue set changed while it was being read"
    for _ in range(3):
        try:
            before = sorted(
                (
                    path.name
                    for path in queue_root.iterdir()
                    if path.is_dir() and path.name.isdigit()
                ),
                key=int,
            )
            queues = []  # type: List[KfdQueueInfo]
            for queue_name in before:
                queue_path = queue_root / queue_name
                gpu_id = _read_optional_int(queue_path / "gpuid")
                if gpu_id is None:
                    raise OSError(
                        f"queue {queue_name} has no readable numeric gpuid"
                    )
                queues.append(
                    KfdQueueInfo(
                        queue_id=int(queue_name),
                        gpu_id=gpu_id,
                        queue_type=_read_optional_int(queue_path / "type"),
                        size=_read_optional_int(queue_path / "size"),
                    )
                )
            after = sorted(
                (
                    path.name
                    for path in queue_root.iterdir()
                    if path.is_dir() and path.name.isdigit()
                ),
                key=int,
            )
            after_gpu_ids = [
                _read_optional_int(queue_root / queue_name / "gpuid")
                for queue_name in after
            ]
            if before == after and after_gpu_ids == [
                queue.gpu_id for queue in queues
            ]:
                return queues
            last_error = "queue set or queue-to-GPU mapping changed while read"
        except OSError as exc:
            last_error = str(exc)
        time.sleep(0.01)

    raise FatalError(
        f"could not obtain a stable KFD queue inventory for PID {pid}: "
        f"{last_error}"
    )


def read_process_queue_gpu_ids(pid: int) -> Dict[int, int]:
    """Compatibility view used by reports and external importers."""

    return {queue.queue_id: queue.gpu_id for queue in read_process_queues(pid)}


def read_all_topology_nodes() -> List[NodeInfo]:
    if not TOPOLOGY_ROOT.is_dir():
        raise FatalError(f"KFD topology metadata is unavailable: {TOPOLOGY_ROOT}")
    nodes = []  # type: List[NodeInfo]
    for node_path in TOPOLOGY_ROOT.iterdir():
        if not node_path.name.isdigit():
            continue
        try:
            nodes.append(resolve_node(int(node_path.name)))
        except FatalError:
            continue
    nodes.sort(key=lambda node: node.node_id)
    return nodes


def discover_gpu_inventory(
    args: argparse.Namespace, queues: Sequence[KfdQueueInfo]
) -> GpuInventory:
    """Join PID queue gpuid values to topology, PCI, render, and AMD-SMI."""

    if not queues:
        raise FatalError(f"PID {args.pid} has no KFD queues")

    visible_nodes = read_all_topology_nodes()
    nodes_by_gpu_id = {node.gpu_id: node for node in visible_nodes}
    nodes_by_bdf = {node.pci_bdf.lower(): node for node in visible_nodes}
    target_gpu_ids = sorted({queue.gpu_id for queue in queues})
    missing_gpu_ids = [
        gpu_id for gpu_id in target_gpu_ids if gpu_id not in nodes_by_gpu_id
    ]
    if missing_gpu_ids:
        raise FatalError(
            "could not map PID KFD queue gpu_id(s) through topology: "
            + ", ".join(str(gpu_id) for gpu_id in missing_gpu_ids)
        )

    try:
        amd_smi_gpu_bdfs = read_amd_smi_gpu_bdfs(args.command_timeout)
    except FatalError:
        if args.gpu_ids:
            raise
        amd_smi_gpu_bdfs = {}
    amd_smi_id_by_bdf = {
        bdf.lower(): gpu_id for gpu_id, bdf in amd_smi_gpu_bdfs.items()
    }
    for node in visible_nodes:
        node.amd_smi_id = amd_smi_id_by_bdf.get(node.pci_bdf.lower())

    target_nodes = [nodes_by_gpu_id[gpu_id] for gpu_id in target_gpu_ids]
    selected_nodes = []  # type: List[NodeInfo]
    if args.all_gpus:
        selected_nodes = list(target_nodes)
    elif args.gpu_ids:
        seen = set()
        for amd_smi_id in args.gpu_ids:
            if amd_smi_id in seen:
                continue
            seen.add(amd_smi_id)
            if amd_smi_id not in amd_smi_gpu_bdfs:
                available = ", ".join(
                    str(item) for item in sorted(amd_smi_gpu_bdfs)
                )
                raise FatalError(
                    f"AMD-SMI GPU {amd_smi_id} does not exist; available IDs: "
                    f"{available}"
                )
            bdf = amd_smi_gpu_bdfs[amd_smi_id].lower()
            node = nodes_by_bdf.get(bdf)
            if node is None:
                raise FatalError(
                    f"AMD-SMI GPU {amd_smi_id} at PCI {bdf} has no matching "
                    "KFD topology GPU node"
                )
            if node.gpu_id not in target_gpu_ids:
                raise FatalError(
                    f"AMD-SMI GPU {amd_smi_id} at PCI {bdf} is not used by "
                    f"PID {args.pid}; Queue Doctor will not dump it"
                )
            selected_nodes.append(node)
    else:
        seen = set()
        for node_id in args.node_ids:
            if node_id in seen:
                continue
            seen.add(node_id)
            node = next(
                (item for item in visible_nodes if item.node_id == node_id),
                None,
            )
            if node is None:
                raise FatalError(f"KFD topology node {node_id} does not exist")
            if node.gpu_id not in target_gpu_ids:
                raise FatalError(
                    f"KFD topology node {node_id} (gpu_id {node.gpu_id}) is "
                    f"not used by PID {args.pid}; Queue Doctor will not dump it"
                )
            selected_nodes.append(node)

    if not selected_nodes:
        raise FatalError(f"no target GPU was selected for PID {args.pid}")
    return GpuInventory(
        queues=list(queues),
        target_nodes=target_nodes,
        selected_nodes=selected_nodes,
        visible_nodes=visible_nodes,
        amd_smi_gpu_bdfs=amd_smi_gpu_bdfs,
    )


def resolve_target_nodes(
    args: argparse.Namespace, queue_gpu_ids: Dict[int, int]
) -> List[NodeInfo]:
    """Compatibility wrapper around authoritative inventory selection."""

    queues = [
        KfdQueueInfo(queue_id, gpu_id)
        for queue_id, gpu_id in sorted(queue_gpu_ids.items())
    ]
    return discover_gpu_inventory(args, queues).selected_nodes


def node_label(node: NodeInfo) -> str:
    amd_smi = (
        str(node.amd_smi_id) if node.amd_smi_id is not None else "unknown"
    )
    return (
        f"AMD-SMI={amd_smi} / KFD node={node.node_id} / "
        f"gpu_id={node.gpu_id} / PCI={node.pci_bdf} / "
        f"renderD{node.render_minor}"
    )


def queues_for_gpu(
    queues: Sequence[KfdQueueInfo], gpu_id: int
) -> List[KfdQueueInfo]:
    return sorted(
        (queue for queue in queues if queue.gpu_id == gpu_id),
        key=lambda queue: queue.queue_id,
    )


def render_target_gpu_inventory(
    pid: int,
    inventory: GpuInventory,
) -> str:
    source = KFD_PROCESS_ROOT / str(pid) / "queues"
    selected_gpu_ids = {node.gpu_id for node in inventory.selected_nodes}
    lines = [
        "Target GPUs",
        "===========",
        (
            "Source of truth: KFD per-process queue metadata "
            f"{source}/*/gpuid; joined by gpu_id through "
            f"{TOPOLOGY_ROOT}/<node> to PCI/render and by PCI BDF to AMD-SMI."
        ),
    ]
    for node in inventory.target_nodes:
        gpu_queues = queues_for_gpu(inventory.queues, node.gpu_id)
        queue_ids = ", ".join(str(queue.queue_id) for queue in gpu_queues)
        lines.append(
            f"  {node_label(node)} / KFD queues={len(gpu_queues)} "
            f"/ KFD queue IDs=[{queue_ids}]"
        )

    lines.extend(["", "Queue Doctor selected GPUs:"])
    for node in inventory.selected_nodes:
        lines.append(f"  DUMP {node_label(node)}")
    excluded_targets = [
        node
        for node in inventory.target_nodes
        if node.gpu_id not in selected_gpu_ids
    ]
    for node in excluded_targets:
        lines.append(f"  SKIP {node_label(node)} (excluded by explicit filter)")

    lines.extend(
        [
            "",
            (
                "Queue ID namespaces: KFD queue IDs above are process-global "
                "sysfs directory IDs. UMR queue IDs are separate 0-based "
                f"indices in the PID section of {KFD_MQDS_PATH}; they are "
                "never joined by numeric equality."
            ),
        ]
    )
    return "\n".join(lines) + "\n"


def without_target_gpu_inventory(
    summary: str,
    inventory_summary: str,
) -> str:
    """Remove a persisted inventory prefix from terminal-oriented output."""
    if not summary.startswith(inventory_summary):
        return summary
    return summary[len(inventory_summary):].lstrip("\n")


def inventory_to_dict(pid: int, inventory: GpuInventory) -> Dict[str, Any]:
    selected_gpu_ids = {node.gpu_id for node in inventory.selected_nodes}
    target_bdfs = {
        node.pci_bdf.lower() for node in inventory.target_nodes
    }
    return {
        "pid": pid,
        "source_of_truth": (
            f"{KFD_PROCESS_ROOT}/{pid}/queues/<kfd_queue_id>/gpuid"
        ),
        "topology_source": str(TOPOLOGY_ROOT),
        "umr_queue_namespace_source": str(KFD_MQDS_PATH),
        "target_gpus": [
            {
                **vars(node),
                "kfd_queue_ids": [
                    queue.queue_id
                    for queue in queues_for_gpu(inventory.queues, node.gpu_id)
                ],
                "selected_for_dump": node.gpu_id in selected_gpu_ids,
            }
            for node in inventory.target_nodes
        ],
        "kfd_queues": [record_to_dict(queue) for queue in inventory.queues],
        "amd_smi_gpu_bdfs": {
            amd_smi_id: bdf
            for amd_smi_id, bdf in inventory.amd_smi_gpu_bdfs.items()
            if bdf.lower() in target_bdfs
        },
    }


def command_prefix(no_sudo: bool) -> List[str]:
    if no_sudo or os.geteuid() == 0:
        return []
    if shutil.which("sudo") is None:
        raise FatalError("UMR needs debugfs access, but sudo was not found")
    validation = subprocess.run(["sudo", "-v"], check=False)
    if validation.returncode != 0:
        raise FatalError("sudo authentication failed")
    return ["sudo", "-n"]


def run_command(argv: Sequence[str], timeout: float) -> CommandResult:
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        completed = subprocess.run(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        return CommandResult(
            list(argv),
            124,
            output + "\n[tool] command timed out\n",
            timed_out=True,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
    return CommandResult(
        list(argv),
        completed.returncode,
        completed.stdout,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc).isoformat(),
    )


def run_binary_command(argv: Sequence[str], timeout: float) -> BinaryCommandResult:
    """Run a command whose stdout is binary and keep stderr separate."""
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        completed = subprocess.run(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return BinaryCommandResult(
            list(argv),
            124,
            exc.stdout or b"",
            exc.stderr or b"",
            timed_out=True,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
    return BinaryCommandResult(
        list(argv),
        completed.returncode,
        completed.stdout,
        completed.stderr,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc).isoformat(),
    )


def write_command_log(path: Path, result: CommandResult) -> None:
    command = " ".join(shlex.quote(str(argument)) for argument in result.argv)
    timing = (
        f"[started_at={result.started_at or '<unavailable>'}]\n"
        f"[finished_at={result.finished_at or '<unavailable>'}]\n"
    )
    path.write_text(
        f"$ {command}\n{timing}[exit={result.returncode}]\n{result.output}",
        encoding="utf-8",
    )


def parse_integer(value: Any) -> int:
    """Parse integer metadata while accepting decimal and prefixed bases."""
    if isinstance(value, int):
        return value
    return int(str(value).strip(), 0)


def extract_pid_mqd_section(output: str, pid: int) -> str:
    process_re = re.compile(r"^Process\s+(\d+)\s+PASID\s+\d+:\s*$")
    active = False
    lines = []  # type: List[str]
    for line in output.splitlines():
        match = process_re.match(line)
        if match:
            if active:
                break
            active = int(match.group(1)) == pid
        if active:
            lines.append(line)
    return "\n".join(lines) + ("\n" if lines else "")


def parse_kfd_mqd_queue_inventory(
    output: str, pid: int
) -> List[MqdQueueIdentity]:
    """Reproduce UMR's KFD MQD ordering while retaining each device gpu_id."""

    section = extract_pid_mqd_section(output, pid)
    queue_re = re.compile(
        r"^\s{2}(Compute|SDMA) queue on device ([0-9a-fA-F]+)\s*$"
    )
    words_re = re.compile(
        r"^\s+([0-9a-fA-F]{8}):((?:\s+[0-9a-fA-F]{8}){1,8})\s*$"
    )
    parsed = []  # type: List[MqdQueueIdentity]
    current = None  # type: Optional[Dict[str, Any]]

    def finish_current() -> None:
        if current is None:
            return
        mqd_gpu_address = None
        words = current["words"]
        if current["queue_type"] == 1 and 128 in words and 129 in words:
            address = words[128] | (words[129] << 32)
            mqd_gpu_address = address or None
        parsed.append(
            MqdQueueIdentity(
                queue_id=len(parsed),
                gpu_id=current["gpu_id"],
                queue_type=current["queue_type"],
                mqd_gpu_address=mqd_gpu_address,
            )
        )

    for line in section.splitlines():
        queue_match = queue_re.match(line)
        if queue_match:
            finish_current()
            current = {
                "queue_type": 1 if queue_match.group(1) == "Compute" else 3,
                "gpu_id": int(queue_match.group(2), 16),
                "words": {},
                "first_dump_complete": False,
            }
            continue
        if current is None:
            continue
        words_match = words_re.match(line)
        if not words_match:
            continue
        byte_offset = int(words_match.group(1), 16)
        if byte_offset == 0 and current["words"]:
            # Multi-XCC queues repeat the MQD from offset zero. UMR keeps
            # only the first dump, so the namespace bridge must do the same.
            current["first_dump_complete"] = True
        if current["first_dump_complete"]:
            continue
        word_offset = byte_offset // 4
        for index, value in enumerate(words_match.group(2).split()):
            current["words"][word_offset + index] = int(value, 16)
    finish_current()
    return parsed


def mqd_inventory_signatures(
    identities: Sequence[MqdQueueIdentity],
) -> List[Tuple[int, int, int, Optional[int]]]:
    return [identity.signature() for identity in identities]


def capture_kfd_mqd_queue_inventory(
    *,
    prefix: Sequence[str],
    pid: int,
    timeout: float,
    path: Path,
) -> List[MqdQueueIdentity]:
    result = run_command(
        [*prefix, "cat", str(KFD_MQDS_PATH)],
        timeout,
    )
    section = extract_pid_mqd_section(result.output, pid)
    path.write_text(
        "$ "
        + " ".join(result.argv)
        + f"\n[exit={result.returncode}]\n"
        + section,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise FatalError(
            f"could not read {KFD_MQDS_PATH} for UMR queue namespace mapping "
            f"(status {result.returncode}); see {path}"
        )
    identities = parse_kfd_mqd_queue_inventory(result.output, pid)
    if not identities:
        raise FatalError(
            f"{KFD_MQDS_PATH} has no queue headers for PID {pid}; see {path}"
        )
    return identities


def map_umr_queues_to_gpu(
    *,
    queues: Sequence[QueueInfo],
    target_gpu_id: int,
    process_gpu_ids: Sequence[int],
    before: Sequence[MqdQueueIdentity],
    after: Sequence[MqdQueueIdentity],
    findings: List[Finding],
    snapshot_index: int,
) -> List[QueueInfo]:
    """Map UMR-local MQD indices without comparing them to KFD sysfs IDs."""

    unique_process_gpu_ids = set(process_gpu_ids)
    if len(unique_process_gpu_ids) == 1:
        selected = list(queues)
        for queue in selected:
            queue.kfd_gpu_id = target_gpu_id
            queue.mapping_source = "single-process-gpu"
        return selected

    if not before or not after:
        findings.append(
            Finding(
                "ERROR",
                "UMR_QUEUE_NAMESPACE_UNAVAILABLE",
                (
                    f"cannot map UMR-local queue indices to KFD gpu_id "
                    f"{target_gpu_id}: no PID-scoped KFD MQD device inventory; "
                    "no queue on this GPU was dumped"
                ),
                snapshot=snapshot_index,
            )
        )
        return []
    if mqd_inventory_signatures(before) != mqd_inventory_signatures(after):
        findings.append(
            Finding(
                "ERROR",
                "KFD_QUEUE_SET_CHANGED",
                (
                    "the PID MQD queue/device order changed while UMR listed "
                    "queues; refusing this snapshot to avoid a cross-GPU dump"
                ),
                snapshot=snapshot_index,
            )
        )
        return []

    identities = {identity.queue_id: identity for identity in before}
    selected = []  # type: List[QueueInfo]
    unrelated_count = 0
    ambiguous_count = 0
    for queue in queues:
        identity = identities.get(queue.queue_id)
        if identity is None:
            ambiguous_count += 1
            findings.append(
                Finding(
                    "ERROR",
                    "UMR_QUEUE_ID_OUTSIDE_MQD_INVENTORY",
                    (
                        "UMR-local queue index is absent from the bracketing "
                        "PID MQD inventory; queue skipped"
                    ),
                    queue.queue_id,
                    snapshot_index,
                )
            )
            continue
        if identity.gpu_id != target_gpu_id:
            unrelated_count += 1
            continue
        type_matches = identity.queue_type == queue.queue_type or (
            identity.queue_type == 1 and queue.queue_type == 2
        )
        if not type_matches:
            ambiguous_count += 1
            findings.append(
                Finding(
                    "ERROR",
                    "UMR_QUEUE_TYPE_IDENTITY_MISMATCH",
                    (
                        f"PID MQD inventory type={identity.queue_type}, UMR "
                        f"type={queue.queue_type}; queue skipped"
                    ),
                    queue.queue_id,
                    snapshot_index,
                )
            )
            continue
        try:
            umr_mqd_address = (
                int(queue.mqd_gpu_address, 0)
                if queue.mqd_gpu_address != "<unavailable>"
                else None
            )
        except ValueError:
            umr_mqd_address = None
        if (
            identity.mqd_gpu_address is not None
            and umr_mqd_address is not None
            and identity.mqd_gpu_address != umr_mqd_address
        ):
            ambiguous_count += 1
            findings.append(
                Finding(
                    "ERROR",
                    "UMR_QUEUE_MQD_ADDRESS_MISMATCH",
                    (
                        f"PID MQD address=0x{identity.mqd_gpu_address:x}, "
                        f"UMR address=0x{umr_mqd_address:x}; queue skipped"
                    ),
                    queue.queue_id,
                    snapshot_index,
                )
            )
            continue
        queue.kfd_gpu_id = identity.gpu_id
        queue.mapping_source = (
            f"{KFD_MQDS_PATH} PID section: ordered index + gpu_id"
        )
        selected.append(queue)

    if snapshot_index == 0:
        findings.append(
            Finding(
                "INFO",
                "UMR_QUEUE_NAMESPACE_MAPPED",
                (
                    f"mapped {len(selected)} UMR-local queue(s) to gpu_id "
                    f"{target_gpu_id} using PID-scoped KFD MQD device headers; "
                    f"excluded {unrelated_count} queue(s) on other GPUs and "
                    f"skipped {ambiguous_count} ambiguous queue(s)"
                ),
                snapshot=snapshot_index,
            )
        )
    return selected


def base_umr_command(
    prefix: Sequence[str], umr: Path, node: NodeInfo, vm_partition: int
) -> List[str]:
    return [
        *prefix,
        str(umr),
        "--by-pci",
        node.pci_bdf,
        "--vm-partition",
        str(vm_partition),
    ]


def parse_sdma_blocks(output: str) -> Dict[int, str]:
    blocks = {}  # type: Dict[int, str]
    block_re = re.compile(
        r"^\s*(?P<path>\S+\.sdma[^.\s{]*\{(?P<instance>\d+)\})\s+\(",
        re.IGNORECASE,
    )
    for line in output.splitlines():
        match = block_re.match(line)
        if not match:
            continue
        instance = int(match.group("instance"))
        path = match.group("path")
        if instance in blocks and blocks[instance] != path:
            raise FatalError(
                f"UMR listed multiple SDMA blocks for instance {instance}: "
                f"{blocks[instance]} and {path}"
            )
        blocks[instance] = path
    return blocks


def parse_queue_list(output: str, pid: int) -> List[QueueInfo]:
    client_re = re.compile(
        r"^Client #.*:\s+comm=\[(?P<comm>.*)\]\s+tgid=(?P<pid>\d+)\s+type=(?P<type>\S+)"
    )
    queue_re = re.compile(
        r"^\s*(?P<selected>\*\*)?\s*queue=(?P<queue>\d+)\s+"
        r"type=(?P<type>\d+)\s+mqd_gpu_addr=(?P<mqd>\S+)\s+"
        r"rptr=(?P<rptr>0x[0-9a-fA-F]+)\s+wptr=(?P<wptr>0x[0-9a-fA-F]+)"
    )
    active = False
    queues = []  # type: List[QueueInfo]
    for line in output.splitlines():
        client_match = client_re.match(line)
        if client_match:
            active = int(client_match.group("pid")) == pid
            continue
        if not active:
            continue
        queue_match = queue_re.match(line)
        if not queue_match:
            continue
        queue_type = int(queue_match.group("type"))
        queues.append(
            QueueInfo(
                queue_id=int(queue_match.group("queue")),
                queue_type=queue_type,
                queue_type_name=QUEUE_TYPE_NAMES.get(
                    queue_type, f"unknown-{queue_type}"
                ),
                mqd_gpu_address=queue_match.group("mqd"),
                rptr=int(queue_match.group("rptr"), 16),
                wptr=int(queue_match.group("wptr"), 16),
                selected=queue_match.group("selected") is not None,
            )
        )
    return queues


def parse_queue_states(output: str) -> List[QueueState]:
    queue_header_re = re.compile(r"^Queue #(?P<queue>\d+):")
    queue_type_re = re.compile(r"^\s*queue_type:\s*(\d+)(?:\s+\(([^)]+)\))?")
    fields = {
        "mqd_gpu_address": re.compile(r"^\s*mqd_gpu_address:\s*(\S+)"),
        "hqd_base_address": re.compile(r"^\s*hqd_base_addr:\s*(\S+)"),
        "hqd_active": re.compile(r"^\s*hqd_active:\s*(0x[0-9a-fA-F]+)"),
        "rb_buf_size_words": re.compile(r"^\s*rb_buf_size:\s*(0x[0-9a-fA-F]+)"),
    }
    metadata_patterns = {
        "doorbell_id": re.compile(
            r"^\s*(?:doorbell(?:_id)?|sdma_rlcx_doorbell_offset):\s*(0x[0-9a-fA-F]+|\d+)"
        ),
        "doorbell_address": re.compile(
            r"^\s*doorbell(?:_address|_addr):\s*(0x[0-9a-fA-F]+|\d+)"
        ),
        "doorbell_value": re.compile(
            r"^\s*doorbell(?:_value|_current):\s*(0x[0-9a-fA-F]+|\d+)"
        ),
        "memory_read_dispatch_id": re.compile(
            r"^\s*(?:memory_)?read_dispatch_id:\s*(0x[0-9a-fA-F]+|\d+)"
        ),
        "memory_write_dispatch_id": re.compile(
            r"^\s*(?:memory_)?write_dispatch_id:\s*(0x[0-9a-fA-F]+|\d+)"
        ),
    }
    pointer_fields = {
        "hqd_rptr": re.compile(
            r"^\s*hqd_rptr_addr:\s*(\S+)\s+\((0x[0-9a-fA-F]+)\)"
        ),
        "wptr_poll": re.compile(
            r"^\s*rb_wptr_poll_addr:\s*(\S+)\s+\((0x[0-9a-fA-F]+)\)"
        ),
    }
    sdma_identity_re = re.compile(
        r"^\s*queue\.(\d+)\.MQD\[\d+\]\s*==\s*"
        r"(0x[0-9a-fA-F]+)\s+\((sdma_engine_id|sdma_queue_id)\)"
    )

    states = []  # type: List[QueueState]
    current = None  # type: Optional[QueueState]
    for line in output.splitlines():
        header_match = queue_header_re.match(line)
        if header_match:
            current = QueueState(queue_id=int(header_match.group("queue")))
            states.append(current)
            continue
        if current is None:
            continue

        sdma_identity_match = sdma_identity_re.match(line)
        if sdma_identity_match:
            queue_id = int(sdma_identity_match.group(1))
            if queue_id == current.queue_id:
                setattr(
                    current,
                    sdma_identity_match.group(3),
                    int(sdma_identity_match.group(2), 16),
                )
            continue
        mqd_named_match = re.match(
            r"^\s*queue\.(\d+)\.MQD\[\d+\]\s*==\s*"
            r"(0x[0-9a-fA-F]+)\s+\(([^)]+)\)", line
        )
        if mqd_named_match and int(mqd_named_match.group(1)) == current.queue_id:
            value = int(mqd_named_match.group(2), 16)
            name = mqd_named_match.group(3).lower()
            if "doorbell" in name and "offset" in name:
                current.doorbell_id = value
            elif name.endswith("doorbell") or "doorbell_address" in name:
                current.doorbell_address = value
            elif "read_dispatch_id" in name:
                current.memory_read_dispatch_id = value
            elif "write_dispatch_id" in name:
                current.memory_write_dispatch_id = value
            elif "compute_current_logic_xcc_id" in name:
                current.compute_current_logic_xcc_id = value
            elif "pm4_target_xcc_in_xcp" in name:
                current.pm4_target_xcc_in_xcp = value
            continue
        metadata_match = None
        for name, pattern in metadata_patterns.items():
            metadata_match = pattern.match(line)
            if metadata_match:
                setattr(current, name, int(metadata_match.group(1), 0))
                break
        if metadata_match:
            continue

        type_match = queue_type_re.match(line)
        if type_match:
            current.queue_type = int(type_match.group(1))
            current.queue_type_name = type_match.group(2)
            continue
        for name, pattern in fields.items():
            match = pattern.match(line)
            if not match:
                continue
            value = match.group(1)
            if name in {"hqd_base_address", "hqd_active", "rb_buf_size_words"}:
                parsed_value = parse_integer(value)
                setattr(current, name, parsed_value)
                if name == "rb_buf_size_words":
                    # UMR's user-queue rb_buf_size is a count of uint32_t
                    # words.  umr_read_user_queue_buffer() multiplies it by
                    # four for GPUVM reads; it is not a byte count.
                    current.ring_size_bytes = parsed_value * 4
            else:
                setattr(current, name, value)
            break
        else:
            rptr_match = pointer_fields["hqd_rptr"].match(line)
            if rptr_match:
                current.hqd_rptr_address = rptr_match.group(1)
                current.hqd_rptr_value = int(rptr_match.group(2), 16)
                current.hardware_rptr = current.hqd_rptr_value
                continue
            wptr_match = pointer_fields["wptr_poll"].match(line)
            if wptr_match:
                current.wptr_poll_address = wptr_match.group(1)
                current.wptr_poll_value = int(wptr_match.group(2), 16)
                current.hardware_wptr = current.wptr_poll_value
    return states


def analyze_mqd_output(output: str, snapshot_index: int) -> List[Finding]:
    findings = []  # type: List[Finding]
    error_re = re.compile(
        r"queue\.(\d+)\.MQD\[\d+\]\s*==\s*"
        r"(0x[0-9a-fA-F]+)\s+\(cp_hqd_error\)"
    )
    for match in error_re.finditer(output):
        queue_id = int(match.group(1))
        error_value = int(match.group(2), 16)
        if error_value:
            findings.append(
                Finding(
                    "ERROR",
                    "CP_HQD_ERROR",
                    f"compute MQD reports cp_hqd_error=0x{error_value:x}",
                    queue_id,
                    snapshot_index,
                )
            )
    return findings


def vm_read_command(base_command: Sequence[str], address: int, size: int,
                    pid: Optional[int] = None, queue_id: Optional[int] = None) -> List[str]:
    """Read process GPUVA through UMR; this deliberately does not use GDB."""
    selector = []
    if pid is not None and queue_id is not None:
        selector = ["--user-queue", f"kfd,pid={pid},queue={queue_id}"]
    return [
        *base_command,
        *selector,
        "--vm-read",
        f"0@0x{address:x}",
        f"0x{size:x}",
    ]


def parse_vm_read_bytes(output: str, expected_size: int) -> bytes:
    """Extract byte-oriented UMR output without trusting diagnostic addresses."""
    data = bytearray()
    for line in output.splitlines():
        if line.startswith("$ ") or line.startswith("[") or "error" in line.lower():
            continue
        payload = line.split(":", 1)[1] if ":" in line else line
        tokens = re.findall(r"(?<![0-9a-fA-F])([0-9a-fA-F]{2})(?![0-9a-fA-F])", payload)
        data.extend(int(token, 16) for token in tokens)
    return bytes(data[:expected_size])


def _u64(data: bytes, offset: int, signed=False) -> int:
    return struct.unpack("<q" if signed else "<Q", data[offset:offset + 8])[0]


def _signal_status(value: int) -> str:
    return "PENDING" if value > 0 else "COMPLETED"


def decode_aql_slot(data: bytes, absolute_address: int, slot: int,
                    logical_index: Optional[int], signal_cache: Dict[int, Any]):
    """Decode one complete AQL slot, retaining uncertainty for INVALID headers."""
    header = struct.unpack("<H", data[:2])[0]
    ptype = header & 0xff
    body_ptype = ptype
    barrier = (header >> 8) & 1
    names = {0: "VENDOR_SPECIFIC", 1: "INVALID", 2: "KERNEL_DISPATCH",
             3: "BARRIER_AND", 4: "AGENT_DISPATCH", 5: "BARRIER_OR"}
    name = names.get(ptype, f"UNKNOWN({ptype})")
    residual_body_type = None
    if ptype == 1:
        setup = struct.unpack("<H", data[2:4])[0]
        wg_x = struct.unpack("<H", data[4:6])[0]
        kernel_object = _u64(data, 32)
        amd_format = setup
        if wg_x >= 1 and kernel_object:
            residual_body_type, body_ptype = "KERNEL_DISPATCH", 2
        elif amd_format == 2:
            residual_body_type, body_ptype = "AMD_BARRIER_VALUE", 0
        elif setup == 0:
            residual_body_type, body_ptype = "BARRIER_AND_OR", 3
        else:
            residual_body_type = "UNKNOWN"

    record = {
        "slot": slot, "absolute_address": absolute_address,
        "logical_index": logical_index, "header": header,
        "packet_type": name, "packet_type_id": header & 0xff,
        "barrier_bit": barrier,
        "heuristic_residual_body": residual_body_type is not None,
        "raw": " ".join(f"{byte:02x}" for byte in data),
    }
    if residual_body_type is not None:
        record["residual_body_type"] = residual_body_type
    if body_ptype == 2:
        record["body"] = {
            "setup": struct.unpack("<H", data[2:4])[0],
            "workgroup": list(struct.unpack("<HHH", data[4:10])),
            "reserved0": struct.unpack("<H", data[10:12])[0],
            "grid": list(struct.unpack("<III", data[12:24])),
            "private_segment_size": struct.unpack("<I", data[24:28])[0],
            "group_segment_size": struct.unpack("<I", data[28:32])[0],
            "kernel_object": _u64(data, 32),
            "kernarg_address": _u64(data, 40),
            "reserved2": _u64(data, 48),
            "completion_signal": _u64(data, 56),
        }
        signals = [record["body"]["completion_signal"]]
    elif body_ptype in (3, 5):
        record["body"] = {
            "reserved0": struct.unpack("<H", data[2:4])[0],
            "reserved1": struct.unpack("<I", data[4:8])[0],
            "dependency_signals": [_u64(data, 8 + i * 8) for i in range(5)],
            "reserved2": _u64(data, 48),
            "completion_signal": _u64(data, 56),
        }
        signals = record["body"]["dependency_signals"] + [record["body"]["completion_signal"]]
    elif body_ptype == 4:
        record["body"] = {
            "agent_dispatch_type": struct.unpack("<H", data[2:4])[0],
            "reserved0": struct.unpack("<I", data[4:8])[0],
            "return_address": _u64(data, 8),
            "args": [_u64(data, 16 + i * 8) for i in range(4)],
            "reserved2": _u64(data, 48),
            "completion_signal": _u64(data, 56),
        }
        signals = [record["body"]["completion_signal"]]
    elif body_ptype == 0 and len(data) >= 64:
        record["body"] = {
            "amd_format": struct.unpack("<H", data[2:4])[0],
            "reserved0": struct.unpack("<I", data[4:8])[0],
            "watch_signal": _u64(data, 8),
            "compare_value": struct.unpack("<q", data[16:24])[0],
            "mask": _u64(data, 24),
            "condition": struct.unpack("<I", data[32:36])[0],
            "reserved1": struct.unpack("<I", data[36:40])[0],
            "reserved2": _u64(data, 40),
            "reserved3": _u64(data, 48),
            "completion_signal": _u64(data, 56),
        }
        signals = [record["body"]["watch_signal"], record["body"]["completion_signal"]]
    else:
        signals = []
    record["signal_handles"] = []
    for handle in signals:
        if not handle or handle in record["signal_handles"]:
            continue
        record["signal_handles"].append(handle)
        info = signal_cache.get(handle)
        if info is not None:
            record.setdefault("signals", {})[f"0x{handle:x}"] = info
    return record


def decode_full_ring(data: bytes, ring_base: int, ring_size: int, rptr: int = None,
                     wptr: int = None, signal_cache: Optional[Dict[int, Any]] = None):
    """Decode every physical 64-byte slot; never treats INVALID body as current."""
    signal_cache = signal_cache or {}
    slots = []
    count = ring_size // 64
    pending = pending_interval_layout(rptr, wptr, count)
    logical_by_slot = {
        physical_slot: logical_index
        for physical_slot, logical_index in zip(
            pending["physical_slots"], pending["logical_indices"]
        )
    }
    for slot in range(count):
        address = ring_base + slot * 64
        record = decode_aql_slot(
            data[slot * 64:(slot + 1) * 64],
            address,
            slot,
            logical_by_slot.get(slot),
            signal_cache,
        )
        record["pending"] = slot in logical_by_slot
        roles = []
        if pending["read_slot"] == slot:
            roles.append("rptr")
        if pending["write_slot"] == slot:
            roles.append("wptr")
        record["pointer_roles"] = roles
        slots.append(record)
    return slots


def pending_interval_layout(
    rptr: Optional[int], wptr: Optional[int], slot_count: int
) -> Dict[str, Any]:
    """Map logical [rptr, wptr) onto unique physical AQL ring slots."""
    result = {
        "rptr": rptr,
        "wptr": wptr,
        "read_slot": None,
        "write_slot": None,
        "pending_count": 0,
        "physical_slots": [],
        "logical_indices": [],
        "wraps": False,
        "valid": True,
        "error": None,
    }
    if rptr is None or wptr is None:
        result.update({
            "valid": False,
            "error": "RPTR/WPTR unavailable",
        })
        return result
    if slot_count <= 0:
        result.update({
            "valid": False,
            "error": "ring has no complete AQL packet slots",
        })
        return result

    read_slot = rptr % slot_count
    write_slot = wptr % slot_count
    result.update({"read_slot": read_slot, "write_slot": write_slot})
    if rptr == wptr:
        return result

    if wptr > rptr:
        pending_count = wptr - rptr
    else:
        # Some UMR/KFD sources expose ring-relative pointers.  Preserve the
        # logical half-open interval by advancing WPTR through one wrap.
        pending_count = (write_slot - read_slot) % slot_count
        if pending_count == 0:
            pending_count = slot_count

    result["pending_count"] = pending_count
    result["wraps"] = wptr < rptr or read_slot + pending_count > slot_count
    covered_count = min(pending_count, slot_count)
    result["physical_slots"] = [
        (read_slot + offset) % slot_count for offset in range(covered_count)
    ]
    result["logical_indices"] = [
        rptr + offset for offset in range(covered_count)
    ]
    if pending_count > slot_count:
        result.update({
            "valid": False,
            "error": (
                f"logical pending count {pending_count} exceeds physical "
                f"capacity {slot_count}"
            ),
        })
    return result


def _signal_read(base_command, handle, timeout, cache, pid=None, queue_id=None):
    if handle in cache:
        return cache[handle]
    if len(cache) >= 64 or handle == 0 or handle & 63:
        result = {"handle": handle, "status": "UNAVAILABLE",
                  "reason": "null, unaligned, or bounded-read limit"}
        cache[handle] = result
        return result
    result = run_binary_command(
        vm_read_command(base_command, handle, 64, pid=pid, queue_id=queue_id),
        timeout,
    )
    raw = result.data
    if len(raw) < 16:
        info = {"handle": handle, "status": "UNAVAILABLE",
                "reason": "UMR VM read failed or stale/unmapped",
                "read_exit": result.returncode}
    else:
        value = struct.unpack("<q", raw[8:16])[0]
        info = {"handle": handle, "value": value, "status": _signal_status(value),
                "read_exit": result.returncode}
    cache[handle] = info
    return info


def full_ring_raw_retention_policy(keep_raw: bool) -> str:
    if keep_raw:
        return (
            "retain successful .bin captures (default); retain available raw "
            "data on decode failure"
        )
    return (
        "discard .bin after successful decode (--discard-full-ring-raw); "
        "retain available raw data on decode failure"
    )


def _full_ring_field_prefix(slot: Dict[str, Any], relative_offset: int) -> str:
    ring_base = slot["absolute_address"] - slot["slot"] * 64
    ring_offset = slot["slot"] * 64 + relative_offset
    return f"[0x0@0x{ring_base:x} + 0x{ring_offset:04x}]"


def _full_ring_raw_value(value: int, width: int) -> str:
    masks = {2: 0xffff, 4: 0xffffffff, 8: (1 << 64) - 1}
    return f"0x{value & masks[width]:0{width * 2}x}".rjust(18)


def _format_full_ring_field(
    slot: Dict[str, Any],
    relative_offset: int,
    width: int,
    label: str,
    value: Any,
    raw_value: Optional[int] = None,
) -> str:
    if raw_value is None:
        raw_value = int(value)
    return (
        f"{_full_ring_field_prefix(slot, relative_offset)}\t"
        f"[{_full_ring_raw_value(raw_value, width)}]\t"
        f"|---> {label}={value}"
    )


def _format_full_ring_signal_detail(
    slot: Dict[str, Any], relative_offset: int, detail: str
) -> str:
    return (
        f"{_full_ring_field_prefix(slot, relative_offset)}\t"
        f"[{'':18}]\t"
        f"|     |---> {detail}"
    )


def _format_full_ring_signal(
    slot: Dict[str, Any],
    relative_offset: int,
    label: str,
    handle: int,
    signals: Dict[str, Any],
) -> List[str]:
    lines = [
        _format_full_ring_field(
            slot, relative_offset, 8, label, f"0x{handle:x}", handle
        )
    ]
    if not handle:
        return lines
    info = signals.get(f"0x{handle:x}")
    if info is None or "value" not in info:
        return lines
    value = info["value"]
    lines.extend([
        _format_full_ring_signal_detail(
            slot,
            relative_offset,
            f"value={value} (0x{value & ((1 << 64) - 1):016x})",
        ),
        _format_full_ring_signal_detail(
            slot, relative_offset, f"status={_signal_status(value)}"
        ),
    ])
    return lines


def _format_full_ring_dependency_signal(
    slot: Dict[str, Any],
    relative_offset: int,
    label: str,
    handle: int,
    signals: Dict[str, Any],
) -> List[str]:
    lines = [
        _format_full_ring_field(
            slot, relative_offset, 8, label, f"0x{handle:x}", handle
        )
    ]
    if not handle:
        return lines
    info = signals.get(f"0x{handle:x}")
    if info is None or "value" not in info:
        return lines
    value = info["value"]
    lines.extend([
        _format_full_ring_signal_detail(
            slot,
            relative_offset,
            f"value={value} (0x{value & ((1 << 64) - 1):016x})",
        ),
        _format_full_ring_signal_detail(
            slot, relative_offset, f"status={_signal_status(value)}"
        ),
    ])
    return lines


def format_full_ring_slot(slot: Dict[str, Any]) -> List[str]:
    header = slot["header"]
    packet_type = slot["packet_type"]
    packet_type_id = slot["packet_type_id"]
    hsa_packet_type = f"HSA_{packet_type}"
    annotations = []
    if slot.get("pending"):
        annotations.append(f"PENDING logical={slot['logical_index']}")
    annotation = f" [{' '.join(annotations)}]" if annotations else ""
    lines = [
        (
            f"{_full_ring_field_prefix(slot, 0)}\t"
            f"[{_full_ring_raw_value(header, 4)}]\t"
            f"Opcode 0x{packet_type_id:x} [{hsa_packet_type}] "
            f"(32 words, type: 0, hdr: 0x{header:x}){annotation}"
        )
    ]

    body = slot.get("body", {})
    signals = slot.get("signals", {})
    body_type = slot.get("residual_body_type", packet_type)
    if body_type == "KERNEL_DISPATCH":
        setup = body.get("setup", 0)
        workgroup = body.get("workgroup", [0, 0, 0])
        grid = body.get("grid", [0, 0, 0])
        lines.extend([
            _format_full_ring_field(
                slot, 2, 2, "setup_dimensions", setup & 0x3, setup
            ),
            _format_full_ring_field(
                slot, 4, 2, "workgroup_size_x", workgroup[0]
            ),
            _format_full_ring_field(
                slot, 6, 2, "workgroup_size_y", workgroup[1]
            ),
            _format_full_ring_field(
                slot, 8, 2, "workgroup_size_z", workgroup[2]
            ),
            _format_full_ring_field(
                slot, 10, 2, "reserved0", body.get("reserved0", 0)
            ),
            _format_full_ring_field(slot, 12, 4, "grid_size_x", grid[0]),
            _format_full_ring_field(slot, 16, 4, "grid_size_y", grid[1]),
            _format_full_ring_field(slot, 20, 4, "grid_size_z", grid[2]),
            _format_full_ring_field(
                slot,
                24,
                4,
                "private_segment_size",
                body.get("private_segment_size", 0),
            ),
            _format_full_ring_field(
                slot,
                28,
                4,
                "group_segment_size",
                body.get("group_segment_size", 0),
            ),
            _format_full_ring_field(
                slot,
                32,
                8,
                "kernel_object",
                f"0x{body.get('kernel_object', 0):x}",
                body.get("kernel_object", 0),
            ),
            _format_full_ring_field(
                slot,
                40,
                8,
                "kernarg_address",
                f"0x{body.get('kernarg_address', 0):x}",
                body.get("kernarg_address", 0),
            ),
            _format_full_ring_field(
                slot,
                48,
                8,
                "reserved2",
                f"0x{body.get('reserved2', 0):x}",
                body.get("reserved2", 0),
            ),
        ])
        lines.extend(
            _format_full_ring_signal(
                slot,
                56,
                "completion_signal",
                body.get("completion_signal", 0),
                signals,
            )
        )
    elif body_type in ("BARRIER_AND", "BARRIER_OR", "BARRIER_AND_OR"):
        lines.extend([
            _format_full_ring_field(
                slot, 2, 2, "reserved0", body.get("reserved0", 0)
            ),
            _format_full_ring_field(
                slot, 4, 4, "reserved1", body.get("reserved1", 0)
            ),
        ])
        for index, handle in enumerate(body.get("dependency_signals", [])):
            lines.extend(
                _format_full_ring_dependency_signal(
                    slot,
                    8 + index * 8,
                    f"dep_signal[{index}]",
                    handle,
                    signals,
                )
            )
        lines.append(
            _format_full_ring_field(
                slot,
                48,
                8,
                "reserved2",
                f"0x{body.get('reserved2', 0):x}",
                body.get("reserved2", 0),
            )
        )
        lines.extend(
            _format_full_ring_signal(
                slot,
                56,
                "completion_signal",
                body.get("completion_signal", 0),
                signals,
            )
        )
    elif body_type == "AGENT_DISPATCH":
        lines.extend([
            _format_full_ring_field(
                slot,
                2,
                2,
                "agent_dispatch_type",
                body.get("agent_dispatch_type", 0),
            ),
            _format_full_ring_field(
                slot, 4, 4, "reserved0", body.get("reserved0", 0)
            ),
            _format_full_ring_field(
                slot,
                8,
                8,
                "return_address",
                f"0x{body.get('return_address', 0):x}",
                body.get("return_address", 0),
            ),
        ])
        for index, argument in enumerate(body.get("args", [])):
            lines.append(
                _format_full_ring_field(
                    slot,
                    16 + index * 8,
                    8,
                    f"arg[{index}]",
                    f"0x{argument:x}",
                    argument,
                )
            )
        lines.append(
            _format_full_ring_field(
                slot,
                48,
                8,
                "reserved2",
                f"0x{body.get('reserved2', 0):x}",
                body.get("reserved2", 0),
            )
        )
        lines.extend(
            _format_full_ring_signal(
                slot,
                56,
                "completion_signal",
                body.get("completion_signal", 0),
                signals,
            )
        )
    elif body_type in ("VENDOR_SPECIFIC", "AMD_BARRIER_VALUE"):
        lines.extend([
            _format_full_ring_field(
                slot, 2, 2, "amd_format", body.get("amd_format", 0)
            ),
            _format_full_ring_field(
                slot, 4, 4, "reserved0", body.get("reserved0", 0)
            ),
        ])
        lines.extend(
            _format_full_ring_signal(
                slot, 8, "watch_signal", body.get("watch_signal", 0), signals
            )
        )
        compare_value = body.get("compare_value", 0)
        lines.extend([
            _format_full_ring_field(
                slot, 16, 8, "compare_value", compare_value, compare_value
            ),
            _format_full_ring_field(
                slot,
                24,
                8,
                "mask",
                f"0x{body.get('mask', 0):x}",
                body.get("mask", 0),
            ),
            _format_full_ring_field(
                slot, 32, 4, "condition", body.get("condition", 0)
            ),
            _format_full_ring_field(
                slot, 36, 4, "reserved1", body.get("reserved1", 0)
            ),
            _format_full_ring_field(
                slot,
                40,
                8,
                "reserved2",
                f"0x{body.get('reserved2', 0):x}",
                body.get("reserved2", 0),
            ),
            _format_full_ring_field(
                slot,
                48,
                8,
                "reserved3",
                f"0x{body.get('reserved3', 0):x}",
                body.get("reserved3", 0),
            ),
        ])
        lines.extend(
            _format_full_ring_signal(
                slot,
                56,
                "completion_signal",
                body.get("completion_signal", 0),
                signals,
            )
        )
    return lines


def _command_option_value(
    command: Sequence[str], option: str
) -> Optional[str]:
    try:
        index = list(command).index(option)
    except ValueError:
        return None
    return str(command[index + 1]) if index + 1 < len(command) else None


def command_with_option_value(
    command: Sequence[str], option: str, value: Any
) -> List[str]:
    """Return a command with one value-taking option replaced or appended."""
    updated = list(command)
    try:
        index = updated.index(option)
    except ValueError:
        updated.extend([option, str(value)])
    else:
        if index + 1 >= len(updated):
            updated.append(str(value))
        else:
            updated[index + 1] = str(value)
    return updated


def queue_vm_command(
    base_command: Sequence[str], queue: QueueInfo, state: Optional[QueueState]
) -> Tuple[List[str], Any, str]:
    """Select the queue's MQD XCC when available, with CLI fallback."""
    configured = _command_option_value(base_command, "--vm-partition")
    configured_value = (
        int(configured, 0) if configured is not None else None
    )
    selected = configured_value
    source = "configured --vm-partition fallback"
    if state is not None:
        if (
            queue.queue_type == 2
            and state.pm4_target_xcc_in_xcp is not None
        ):
            selected = state.pm4_target_xcc_in_xcp
            source = "--print-uq pm4_target_xcc_in_xcp"
        elif state.compute_current_logic_xcc_id is not None:
            selected = state.compute_current_logic_xcc_id
            source = "--print-uq compute_current_logic_xcc_id"
    if selected is None:
        return list(base_command), None, source
    return (
        command_with_option_value(base_command, "--vm-partition", selected),
        selected,
        source,
    )


_UMR_AQL_HEADER_RE = re.compile(
    r"^\[0x[0-9a-fA-F]+@(?P<base>0x[0-9a-fA-F]+)\s+\+\s+"
    r"(?P<offset>0x[0-9a-fA-F]+)\]\s+"
    r"\[\s*(?P<header>0x[0-9a-fA-F]+)\]\s+"
    r"Opcode\s+\S+\s+\[HSA_(?P<packet_type>[^\]]+)\]",
    re.MULTILINE,
)


def parse_umr_aql_packet_headers(output: str) -> List[Dict[str, Any]]:
    """Parse ordered AQL packet addresses and 16-bit headers from UMR text."""
    records = []
    for match in _UMR_AQL_HEADER_RE.finditer(output):
        base = int(match.group("base"), 16)
        relative_offset = int(match.group("offset"), 16)
        records.append({
            "display_address": base + relative_offset,
            "display_base": base,
            "relative_offset": relative_offset,
            # UMR's AQL decoder prints the 16-bit packet header on the Opcode
            # line.  The upper half of the raw first dword is the first
            # packet-type-specific body field (for example setup_dimensions),
            # not part of the AQL header.
            "header": int(match.group("header"), 16) & 0xFFFF,
            "packet_type": match.group("packet_type"),
        })
    return records


def correlate_pending_dump(
    output: str,
    *,
    ring_base: int,
    ring_size: int,
    pending: Dict[str, Any],
    slots: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Correlate native pending headers to canonical full-ring physical slots."""
    native = parse_umr_aql_packet_headers(output)
    expected_slots = pending.get("physical_slots", [])
    full_by_slot = {slot["slot"]: slot for slot in slots}
    mismatches = []
    executable_seen = 0
    executable_matched = 0
    compared = min(len(native), len(expected_slots))
    for ordinal in range(compared):
        record = native[ordinal]
        physical_slot = expected_slots[ordinal]
        full_slot = full_by_slot.get(physical_slot)
        expected_address = ring_base + physical_slot * 64
        canonical_address = ring_base + (
            (record["display_address"] - ring_base) % ring_size
        )
        full_header = None
        full_packet_type = None
        if full_slot is not None:
            full_header = int.from_bytes(
                bytes.fromhex(full_slot["raw"])[:2], "little"
            )
            full_packet_type = full_slot["packet_type"]
        executable = record["packet_type"] != "INVALID"
        if executable:
            executable_seen += 1
        matches = (
            full_slot is not None
            and canonical_address == expected_address
            and record["header"] == full_header
            and record["packet_type"] == full_packet_type
        )
        if executable and matches:
            executable_matched += 1
        if not matches and len(mismatches) < 20:
            mismatches.append({
                "ordinal": ordinal,
                "logical_index": (
                    pending.get("logical_indices", [])[ordinal]
                    if ordinal < len(pending.get("logical_indices", []))
                    else None
                ),
                "physical_slot": physical_slot,
                "expected_address": expected_address,
                "native_canonical_address": canonical_address,
                "native_header": record["header"],
                "full_header": full_header,
                "native_packet_type": record["packet_type"],
                "full_packet_type": full_packet_type,
            })
    count_match = len(native) == len(expected_slots)
    exact = pending.get("valid", False) and count_match and not mismatches
    if not slots:
        status = "unavailable"
    else:
        status = "exact" if exact else "mismatch"
    return {
        "status": status,
        "atomic": False,
        "atomicity": (
            "non-atomic: native --dump-uq and full-ring VM read are "
            "separate live commands"
        ),
        "comparison": (
            "canonical address + 16-bit AQL header + decoded packet type; "
            "native --dump-uq does not expose raw packet bodies"
        ),
        "native_packet_count": len(native),
        "expected_packet_count": len(expected_slots),
        "compared_packet_count": compared,
        "executable_native_packets": executable_seen,
        "executable_packets_matched": executable_matched,
        "all_executable_packets_matched": (
            exact and executable_seen == executable_matched
        ),
        "mismatches": mismatches,
    }


def _format_slot_runs(slots: Sequence[int]) -> str:
    if not slots:
        return "<empty>"
    runs = []
    start = previous = slots[0]
    for slot in slots[1:]:
        if slot == previous + 1:
            previous = slot
            continue
        runs.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = slot
    runs.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(runs)


def capture_full_ring(*, base_command, pid, queue, state, output_dir, snapshot_index,
                      timeout, findings, keep_raw=True, pending_result=None,
                      vm_partition_source=None):
    """Capture and fully decode an MQD/HQD ring, retaining raw bytes by default."""
    stem = f"snapshot-{snapshot_index:02d}-queue-{queue.queue_id}-{queue.queue_type_name}"
    raw_path = output_dir / "full-ring" / f"{stem}.bin"
    report_path = output_dir / "full-ring" / f"{stem}.decoded.txt"
    pci_bdf = _command_option_value(base_command, "--by-pci")
    vm_partition_text = _command_option_value(base_command, "--vm-partition")
    try:
        vm_partition = (
            int(vm_partition_text, 0) if vm_partition_text is not None else None
        )
    except ValueError:
        vm_partition = vm_partition_text
    selector = f"kfd,pid={pid},queue={queue.queue_id}"
    capture_context = {
        "gpu_pci_bdf": pci_bdf,
        "kfd_gpu_id": queue.kfd_gpu_id,
        "umr_queue_id": queue.queue_id,
        "user_queue_selector": selector,
        "vm_partition": vm_partition,
        "xcc_context_source": (
            vm_partition_source or "--vm-partition"
        ),
        "mqd_compute_current_logic_xcc_id": (
            state.compute_current_logic_xcc_id
        ),
        "mqd_pm4_target_xcc_in_xcp": state.pm4_target_xcc_in_xcp,
        "vmid": 0,
        "vm_hub": "gfx",
        "address_space": "process GPUVA from UMR --user-queue context",
    }
    meta = {"queue_id": queue.queue_id, "capture_command": None,
            "path": str(report_path), "decoded_path": str(report_path),
            "raw_capture_path": str(raw_path),
            "raw_path": None, "raw_retained": False,
            "raw_retention_reason": None,
            "raw_retention_policy": full_ring_raw_retention_policy(keep_raw),
            "capture_context": capture_context,
            "ring_base_source": "--print-uq hqd_base_addr",
            "ring_capacity_source": "--print-uq rb_buf_size dwords * 4",
            "ring_base": state.hqd_base_address,
            "rb_buf_size_words": state.rb_buf_size_words,
            "ring_size_bytes": state.ring_size_bytes,
            "byte_count": 0, "sha256": None, "exit": None, "timed_out": False,
            "error": None, "decode_complete": False,
            "capture_started_at": None, "capture_finished_at": None,
            "capture_atomic": False}
    if state.hqd_base_address is None or not state.ring_size_bytes:
        meta["error"] = "authoritative hqd_base_addr/rb_buf_size unavailable"
        findings.append(Finding("WARNING", "FULL_RING_METADATA_UNAVAILABLE",
                                meta["error"], queue.queue_id, snapshot_index))
        report_path.write_text("FULL RING UNAVAILABLE: " + meta["error"] + "\n", encoding="utf-8")
        return meta
    base_address = parse_integer(state.hqd_base_address)
    ring_size = parse_integer(state.ring_size_bytes)
    ring_words = (
        parse_integer(state.rb_buf_size_words)
        if state.rb_buf_size_words is not None
        else ring_size // 4
    )
    slot_count = ring_size // 64
    capture_rptr = (
        state.hqd_rptr_value
        if state.hqd_rptr_value is not None
        else queue.rptr
    )
    capture_wptr = (
        state.wptr_poll_value
        if state.wptr_poll_value is not None
        else queue.wptr
    )
    pointer_source = (
        "--print-uq hqd_rptr_addr/rb_wptr_poll_addr values"
        if state.hqd_rptr_value is not None and state.wptr_poll_value is not None
        else "--list-uq rptr/wptr fallback"
    )
    pending = pending_interval_layout(capture_rptr, capture_wptr, slot_count)
    meta.update({
        "ring_base": base_address,
        "rb_buf_size_words": ring_words,
        "ring_size_bytes": ring_size,
        "ring_slot_capacity": slot_count,
        "list_rptr": queue.rptr,
        "list_wptr": queue.wptr,
        "capture_rptr": capture_rptr,
        "capture_wptr": capture_wptr,
        "pointer_source": pointer_source,
        "pointer_sources_match": (
            queue.rptr == capture_rptr and queue.wptr == capture_wptr
        ),
        "pending_interval": pending,
    })
    if not meta["pointer_sources_match"]:
        findings.append(Finding(
            "WARNING",
            "FULL_RING_POINTER_SNAPSHOT_CHANGED",
            (
                f"--list-uq pointers 0x{queue.rptr:x}/0x{queue.wptr:x} differ "
                f"from --print-uq pointers 0x{capture_rptr:x}/"
                f"0x{capture_wptr:x}; full-ring annotations use --print-uq"
            ),
            queue.queue_id,
            snapshot_index,
        ))
    if not pending["valid"]:
        findings.append(Finding(
            "ERROR",
            "FULL_RING_PENDING_INTERVAL_INVALID",
            pending["error"],
            queue.queue_id,
            snapshot_index,
        ))
    command = vm_read_command(
        base_command, base_address, ring_size,
        pid=pid, queue_id=queue.queue_id,
    )
    meta["capture_command"] = command
    result = run_binary_command(command, timeout)
    meta.update({
        "exit": result.returncode,
        "timed_out": result.timed_out,
        "capture_started_at": result.started_at,
        "capture_finished_at": result.finished_at,
    })
    data = result.data
    if data:
        raw_path.write_bytes(data)
        meta["byte_count"] = len(data)
        meta["sha256"] = hashlib.sha256(data).hexdigest()
    if len(data) != ring_size:
        meta["error"] = (
            "UMR VM read produced no bytes"
            if not data
            else f"VM read size mismatch: {len(data)}/{ring_size} bytes"
        )
        findings.append(Finding("WARNING", "FULL_RING_CAPTURE_FAILED", meta["error"],
                                queue.queue_id, snapshot_index))
    cache = {}
    slots = []
    if data and ring_size % 64:
        meta["error"] = f"ring size {ring_size} is not a multiple of 64 bytes"
    elif len(data) == ring_size and len(data) >= 64:
        try:
            slots = decode_full_ring(
                data,
                base_address,
                ring_size,
                capture_rptr,
                capture_wptr,
                cache,
            )
            for slot in slots:
                for handle in slot.get("signal_handles", []):
                    _signal_read(
                        base_command, handle, timeout, cache, pid, queue.queue_id
                    )
                for handle in slot.get("signal_handles", []):
                    slot.setdefault("signals", {})[f"0x{handle:x}"] = cache[handle]
        except (OSError, ValueError, struct.error) as exc:
            meta["error"] = f"full-ring decode failed: {exc}"
            slots = []
    expected_slots = slot_count
    decoded_pending_slots = [
        slot["slot"] for slot in slots if slot.get("pending")
    ]
    expected_pending_slots = pending["physical_slots"]
    missing_pending_slots = [
        slot for slot in expected_pending_slots if slot not in decoded_pending_slots
    ]
    pending_coverage_complete = (
        pending["valid"]
        and len(slots) == expected_slots
        and not missing_pending_slots
        and len(decoded_pending_slots) == len(expected_pending_slots)
    )
    pending_records = [slot for slot in slots if slot.get("pending")]
    pending_invalid_count = sum(
        slot["packet_type"] == "INVALID" for slot in pending_records
    )
    meta.update({
        "pending_slots_expected": expected_pending_slots,
        "pending_slots_decoded": decoded_pending_slots,
        "pending_slots_missing": missing_pending_slots,
        "pending_coverage_complete": pending_coverage_complete,
        "pending_invalid_headers": pending_invalid_count,
        "pending_all_invalid": bool(
            pending_records
            and pending_invalid_count == len(pending_records)
        ),
    })
    if pending_result is not None:
        correlation = correlate_pending_dump(
            pending_result.output,
            ring_base=base_address,
            ring_size=ring_size,
            pending=pending,
            slots=slots,
        )
        correlation.update({
            "native_capture_started_at": pending_result.started_at,
            "native_capture_finished_at": pending_result.finished_at,
            "full_ring_capture_started_at": result.started_at,
            "full_ring_capture_finished_at": result.finished_at,
        })
        meta["pending_correlation"] = correlation
        if correlation["status"] == "mismatch":
            findings.append(Finding(
                "WARNING",
                "PENDING_FULL_RING_MISMATCH",
                (
                    f"native pending dump matched "
                    f"{correlation['executable_packets_matched']}/"
                    f"{correlation['executable_native_packets']} executable "
                    "packet header(s) in the later full-ring VM read; captures are "
                    "non-atomic, so a live producer/consumer race is possible"
                ),
                queue.queue_id,
                snapshot_index,
            ))
    if missing_pending_slots:
        findings.append(Finding(
            "ERROR",
            "FULL_RING_PENDING_SLOTS_MISSING",
            (
                "full-ring decode omitted pending physical slot(s): "
                + _format_slot_runs(missing_pending_slots)
            ),
            queue.queue_id,
            snapshot_index,
        ))
    if meta["pending_all_invalid"]:
        findings.append(Finding(
            "WARNING",
            "FULL_RING_PENDING_ALL_INVALID",
            (
                f"all {len(pending_records)} captured slots in logical "
                "[RPTR, WPTR) have authoritative HSA_INVALID headers; verify "
                "the recorded PCI/VM-partition/user-queue context and compare "
                "the same snapshot's pending dump to distinguish a producer "
                "race or stale/wrong-context data"
            ),
            queue.queue_id,
            snapshot_index,
        ))
    if (
        meta["error"] is None
        and len(data) == ring_size
        and len(slots) == expected_slots
    ):
        meta["decode_complete"] = True
    elif meta["error"] is None:
        meta["error"] = (
            f"incomplete full-ring decode: decoded {len(slots)}/{expected_slots} slots"
        )
    if meta["error"] and not any(
        finding.code == "FULL_RING_CAPTURE_FAILED"
        and finding.queue_id == queue.queue_id
        and finding.snapshot == snapshot_index
        for finding in findings
    ):
        findings.append(Finding(
            "WARNING", "FULL_RING_CAPTURE_FAILED", meta["error"],
            queue.queue_id, snapshot_index,
        ))
    had_raw = raw_path.exists()
    if had_raw and meta["decode_complete"] and not keep_raw:
        try:
            raw_path.unlink()
        except OSError as exc:
            meta["raw_path"] = str(raw_path)
            meta["raw_retained"] = True
            meta["raw_retention_reason"] = f"raw cleanup failed: {exc}"
            findings.append(Finding(
                "WARNING", "FULL_RING_RAW_CLEANUP_FAILED",
                meta["raw_retention_reason"], queue.queue_id, snapshot_index,
            ))
    if raw_path.exists():
        meta["raw_path"] = str(raw_path)
        meta["raw_retained"] = True
        if meta["raw_retention_reason"] is None:
            meta["raw_retention_reason"] = (
                "successful captures are retained by default"
                if meta["decode_complete"]
                else meta["error"] or "decode did not complete"
            )
        raw_disposition = (
            f"retained at {raw_path}: {meta['raw_retention_reason']}"
        )
    elif had_raw and meta["decode_complete"]:
        raw_disposition = (
            "discarded after successful decode by --discard-full-ring-raw"
        )
    else:
        raw_disposition = "not retained because no raw bytes were captured"
    read_slot = pending["read_slot"]
    write_slot = pending["write_slot"]
    pending_count = pending["pending_count"]
    device = (
        f"KFD gpu_id {queue.kfd_gpu_id}"
        if queue.kfd_gpu_id is not None
        else "<unavailable>"
    )
    lines = [
        "==========================================================================",
        (
            f"Queue AMDGPU Queue {device}:{queue.queue_id} "
            f"(device {device}, QID {queue.queue_id}) "
            f"[HSA/{queue.queue_type_name}]"
        ),
        f"  Ring base:   0x{base_address:x}",
        f"  Ring size:   {ring_size} bytes ({slot_count} packet slots)",
        (
            f"  UMR rb_buf_size: {ring_words} dwords "
            f"(converted to {ring_size} bytes)"
        ),
        (
            f"  Read index:  {capture_rptr} (slot {read_slot}) "
            f"[@ {format_optional(state.hqd_rptr_address)}]"
        ),
        (
            f"  Write index: {capture_wptr} (slot {write_slot}) "
            f"[@ {format_optional(state.wptr_poll_address)}]"
        ),
        f"  Pending:     {pending_count} packet(s)",
        (
            "  Pending physical slots: "
            f"{_format_slot_runs(expected_pending_slots)} "
            f"(wraparound={pending['wraps']})"
        ),
        (
            f"  Pending coverage: {len(decoded_pending_slots)} / "
            f"{len(expected_pending_slots)} physical slot(s); "
            f"complete={pending_coverage_complete}"
        ),
        f"  Pointer source: {pointer_source}",
        (
            "  Capture atomicity: non-atomic (native pending dump and "
            "full-ring VM read are separate live commands)"
        ),
        (
            f"  GPU/VM context: PCI={pci_bdf or '<unavailable>'}, "
            f"KFD gpu_id={format_optional(queue.kfd_gpu_id)}, "
            f"VM partition/XCC={format_optional(vm_partition)}, "
            f"hub=gfx, VMID=0"
        ),
        (
            "  MQD XCC metadata: compute_current_logic_xcc_id="
            f"{format_optional(state.compute_current_logic_xcc_id)}, "
            "pm4_target_xcc_in_xcp="
            f"{format_optional(state.pm4_target_xcc_in_xcp)}"
        ),
        f"  UMR user-queue context: {selector}",
        f"  Doorbell ID:      {format_optional(state.doorbell_id)}",
        f"  Doorbell address: {format_optional(state.doorbell_address)}",
        f"  Doorbell value:   {format_optional(state.doorbell_value)}",
        f"  HQD active:       {format_optional(state.hqd_active)}",
        f"  HQD RPTR value:   {format_optional(state.hqd_rptr_value)}",
        f"  WPTR poll value:  {format_optional(state.wptr_poll_value)}",
        (
            "  Hardware RPTR/WPTR: "
            f"{format_optional(state.hardware_rptr)}/"
            f"{format_optional(state.hardware_wptr)}"
        ),
        (
            "  Memory read/write dispatch IDs: "
            f"{format_optional(state.memory_read_dispatch_id)}/"
            f"{format_optional(state.memory_write_dispatch_id)}"
        ),
        f"  Decoded slots: {len(slots)} / {expected_slots}",
        "==========================================================================",
        "",
        "  Full physical-slot dump. HSA_INVALID is the authoritative packet type.",
        "  Heuristic residual-body fields do not establish ownership or state.",
        (
            "  Slots outside [RPTR, WPTR) are not evidence of completed work; "
            "the consumer/runtime may invalidate or reuse them."
        ),
        (
            "  [PENDING logical=N] marks every decoded physical slot in the "
            "logical [RPTR, WPTR) interval."
        ),
        "",
        "Capture command: " + (
            " ".join(shlex.quote(str(item)) for item in command)
            if command else "<unavailable>"
        ),
        f"Capture started: {result.started_at or '<unavailable>'}",
        f"Capture finished: {result.finished_at or '<unavailable>'}",
        f"Capture exit: {result.returncode}; timeout: {result.timed_out}; "
        f"error: {meta['error'] or '<none>'}",
        f"Captured byte count: {len(data)} / {ring_size}",
        f"SHA-256: {meta['sha256'] or '<unavailable>'}",
        f"Raw retention policy: {meta['raw_retention_policy']}",
        f"Raw VM-read artifact: {raw_path}",
        f"Decoded artifact: {report_path}",
        f"Raw VM-read bytes: {raw_disposition}",
        (
            "Pending/full-ring correlation: "
            + (
                f"{meta['pending_correlation']['status']}; executable "
                f"{meta['pending_correlation']['executable_packets_matched']}/"
                f"{meta['pending_correlation']['executable_native_packets']} "
                "headers matched; non-atomic"
                if "pending_correlation" in meta
                else "<not collected>"
            )
        ),
        "",
        (
            f"Dumping 0x{ring_words:x} words from user queue-{queue.queue_id} "
            f"(complete physical ring, from word 0x0 to 0x{ring_words:x}):"
        ),
        (
            f"Decoding IB at 0x0@0x{base_address:x} "
            f"from 0x0@0x{base_address:x} of 0x{ring_size // 2:x} words (type 0)"
        ),
    ]
    for slot in slots:
        lines.extend(format_full_ring_slot(slot))
    if not slots and len(data) < 64:
        lines.append("No complete 64-byte slots decoded.")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    meta["decoded_slots"] = len(slots)
    meta["signal_reads"] = len(cache)
    meta["slots"] = slots
    if not raw_path.exists() and not meta["decode_complete"]:
        meta["raw_retention_reason"] = "no raw bytes were captured"
    return meta


def format_optional(value):
    return "<unavailable>" if value is None else (
        f"0x{value:x}" if isinstance(value, int) else str(value))


def analyze_packet_dump(
    queue: QueueInfo,
    mode: str,
    path: Path,
    result: CommandResult,
    snapshot_index: int,
    timeout_seconds: Optional[float] = None,
) -> Tuple[PacketDump, List[Finding]]:
    output = result.output
    idle = IDLE_QUEUE_TEXT in output
    declared_match = re.search(
        r"Dumping\s+(0x[0-9a-fA-F]+)\s+words", output, re.IGNORECASE
    )
    declared_words = int(declared_match.group(1), 16) if declared_match else None
    packet_headers = re.findall(
        r"\bOpcode\s+0x[0-9a-fA-F]+(?:/0x[0-9a-fA-F]+)?\s+"
        r"\[([^\]]+)\]\s+\((\d+)\s+words",
        output,
    )
    packet_words = [int(value) for _, value in packet_headers]
    packet_types = {}  # type: Dict[str, int]
    for packet_name, _ in packet_headers:
        packet_types[packet_name] = packet_types.get(packet_name, 0) + 1
    decoder_completed = "Done decoding IB" in output
    dump = PacketDump(
        queue_id=queue.queue_id,
        mode=mode,
        path=str(path),
        command_status=result.returncode,
        idle=idle,
        declared_words=declared_words,
        decoded_packets=len(packet_words),
        decoded_packet_words=sum(packet_words),
        packet_types=packet_types,
        decoder_completed=decoder_completed,
        timeout_seconds=timeout_seconds,
        timed_out=result.timed_out,
        command=result.argv,
        started_at=result.started_at,
        finished_at=result.finished_at,
    )
    dump.declared_word_unit = "32-bit dwords"
    dump.declared_bytes = (
        declared_words * 4 if declared_words is not None else None
    )
    dump.decoded_packet_word_unit = (
        "16-bit decoder words" if queue.queue_type == 1 else "32-bit dwords"
    )
    dump.decoded_packet_bytes = sum(packet_words) * (
        2 if queue.queue_type == 1 else 4
    )
    findings = []  # type: List[Finding]

    filtered_output = output.replace(
        "[ERROR]: The user queue's RPTR and WPTR are equal.", ""
    )
    matched_errors = [
        pattern.pattern
        for pattern in DECODE_ERROR_PATTERNS
        if pattern.search(filtered_output)
    ]
    if result.returncode != 0 and not idle:
        findings.append(
            Finding(
                "ERROR",
                "UMR_DUMP_FAILED",
                f"UMR {mode} packet dump exited with status {result.returncode}",
                queue.queue_id,
                snapshot_index,
            )
        )
    if matched_errors:
        findings.append(
            Finding(
                "ERROR",
                "PACKET_DECODE_ERROR",
                f"UMR reported a packet/read error in {mode} dump",
                queue.queue_id,
                snapshot_index,
            )
        )
    if mode == "pending" and queue.queue_type == 1 and "HSA_INVALID" in output:
        findings.append(
            Finding(
                "WARNING",
                "INVALID_AQL_PACKET",
                (
                    "an HSA_INVALID packet is present between live RPTR and WPTR; "
                    "repeat the snapshot to exclude a producer/reader race"
                ),
                queue.queue_id,
                snapshot_index,
            )
        )
    if declared_words and not packet_words:
        findings.append(
            Finding(
                "WARNING",
                "NO_DECODED_PACKETS",
                f"UMR read {declared_words} words but decoded no packet headers",
                queue.queue_id,
                snapshot_index,
            )
        )
    if packet_words and any(words <= 0 for words in packet_words):
        findings.append(
            Finding(
                "ERROR",
                "INVALID_PACKET_LENGTH",
                "UMR decoded a packet with a non-positive word count",
                queue.queue_id,
                snapshot_index,
            )
        )
    if (
        declared_words
        and queue.queue_type != 1
        and sum(packet_words) > declared_words
    ):
        findings.append(
            Finding(
                "WARNING",
                "PACKET_LENGTH_OVERRUN",
                (
                    f"decoded packet lengths total {sum(packet_words)} words, "
                    f"larger than the {declared_words}-word live snapshot"
                ),
                queue.queue_id,
                snapshot_index,
            )
        )
    if declared_words and not decoder_completed:
        findings.append(
            Finding(
                "WARNING",
                "DECODER_DID_NOT_FINISH",
                "UMR began decoding a non-empty queue but did not report completion",
                queue.queue_id,
                snapshot_index,
            )
        )
    return dump, findings


def analyze_snapshot_state(snapshot: Snapshot) -> List[Finding]:
    findings = []  # type: List[Finding]
    state_by_id = {state.queue_id: state for state in snapshot.states}
    for queue in snapshot.queues:
        state = state_by_id.get(queue.queue_id)
        if state is None:
            continue
        if state.queue_type is not None and state.queue_type != queue.queue_type:
            findings.append(
                Finding(
                    "ERROR",
                    "QUEUE_TYPE_MISMATCH",
                    (
                        f"list-uq type={queue.queue_type}, but print-uq "
                        f"type={state.queue_type}"
                    ),
                    queue.queue_id,
                    snapshot.index,
                )
            )
        if queue.pending and state.ring_size_bytes == 0:
            findings.append(
                Finding(
                    "WARNING",
                    "MISSING_RING_SIZE",
                    "queue has pending pointers but print-uq reports a zero ring size",
                    queue.queue_id,
                    snapshot.index,
                )
            )
        if (
            state.ring_size_bytes
            and state.ring_size_bytes & (state.ring_size_bytes - 1)
        ):
            findings.append(
                Finding(
                    "ERROR",
                    "INVALID_RING_SIZE",
                    (
                        f"ring size 0x{state.ring_size_bytes:x} is not a "
                        "power of two"
                    ),
                    queue.queue_id,
                    snapshot.index,
                )
            )
        if (
            queue.pending
            and queue.queue_type in (2, 3)
            and state.ring_size_bytes
        ):
            capacity_words = state.ring_size_bytes // 4
            pointer_delta = queue.wptr - queue.rptr
            if pointer_delta >= capacity_words:
                findings.append(
                    Finding(
                        "ERROR",
                        "QUEUE_POINTER_OVERRUN",
                        (
                            f"WPTR-RPTR={pointer_delta} words is not smaller "
                            f"than ring capacity {capacity_words} words"
                        ),
                        queue.queue_id,
                        snapshot.index,
                    )
                )
        if queue.pending and state.hqd_active == 0:
            findings.append(
                Finding(
                    "INFO",
                    "QUEUE_NOT_MAPPED",
                    (
                        "queue has pending work while HQD is inactive; this can be "
                        "normal when HWS has temporarily descheduled the queue"
                    ),
                    queue.queue_id,
                    snapshot.index,
                )
            )
    return findings


def analyze_progress(snapshots: Sequence[Snapshot]) -> List[Finding]:
    if len(snapshots) < 2:
        return []
    histories = {}  # type: Dict[Tuple[int, int], List[QueueInfo]]
    for snapshot in snapshots:
        for queue in snapshot.queues:
            histories.setdefault((queue.queue_id, queue.queue_type), []).append(queue)

    findings = []  # type: List[Finding]
    for (queue_id, _), history in histories.items():
        if len(history) != len(snapshots):
            findings.append(
                Finding(
                    "INFO",
                    "QUEUE_SET_CHANGED",
                    "queue appeared or disappeared during sampling",
                    queue_id,
                )
            )
            continue
        if all(queue.pending for queue in history):
            pointers = {(queue.rptr, queue.wptr) for queue in history}
            if len(pointers) == 1:
                findings.append(
                    Finding(
                        "WARNING",
                        "QUEUE_NO_PROGRESS",
                        (
                            f"RPTR/WPTR stayed at 0x{history[0].rptr:x}/"
                            f"0x{history[0].wptr:x} with pending work across "
                            f"{len(history)} samples; long-running packets can "
                            "produce this heuristic warning"
                        ),
                        queue_id,
                    )
                )
            elif (
                len({queue.rptr for queue in history}) == 1
                and history[-1].wptr > history[0].wptr
            ):
                findings.append(
                    Finding(
                        "WARNING",
                        "QUEUE_BACKLOG_GROWING",
                        (
                            "WPTR advanced while RPTR did not move across samples; "
                            "the queue backlog may be growing"
                        ),
                        queue_id,
                    )
                )
    return findings


def make_output_dir(args: argparse.Namespace, nodes: Sequence[NodeInfo]) -> Path:
    if args.output_dir is not None:
        output_dir = args.output_dir
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if len(nodes) == 1:
            if nodes[0].amd_smi_id is not None:
                suffix = f"gpu{nodes[0].amd_smi_id}"
            else:
                suffix = f"node{nodes[0].node_id}"
        else:
            suffix = f"gpus{len(nodes)}"
        output_dir = (
            DEFAULT_OUTPUT_ROOT
            / f"umr_queue_doctor_pid{args.pid}_{suffix}_{stamp}"
        )
    output_dir.mkdir(parents=True, exist_ok=False)
    return output_dir.resolve()


def discover_sdma_blocks(
    base_command: Sequence[str],
    output_dir: Path,
    command_timeout: float,
) -> Tuple[Dict[int, str], Path]:
    result = run_command(
        [*base_command, "--list-blocks"],
        command_timeout,
    )
    path = output_dir / "list-blocks.txt"
    write_command_log(path, result)
    if result.returncode != 0:
        raise FatalError(
            f"UMR --list-blocks failed with status {result.returncode}; "
            f"see {path}"
        )
    blocks = parse_sdma_blocks(result.output)
    if not blocks:
        raise FatalError(
            f"UMR --list-blocks found no SDMA IP blocks; see {path}"
        )
    return blocks, path


def capture_snapshot(
    *,
    index: int,
    args: argparse.Namespace,
    target_gpu_id: int,
    process_gpu_ids: Sequence[int],
    prefix: Sequence[str],
    output_dir: Path,
    base_command: Sequence[str],
    sdma_blocks: Dict[int, str],
    findings: List[Finding],
) -> Snapshot:
    timestamp = datetime.now(timezone.utc).isoformat()
    before = []  # type: List[MqdQueueIdentity]
    if len(set(process_gpu_ids)) > 1:
        before = capture_kfd_mqd_queue_inventory(
            prefix=prefix,
            pid=args.pid,
            timeout=args.command_timeout,
            path=output_dir / f"snapshot-{index:02d}-kfd-mqds-before.txt",
        )
    list_result = run_command([*base_command, "--list-uq"], args.command_timeout)
    list_path = output_dir / f"snapshot-{index:02d}-list-uq.txt"
    write_command_log(list_path, list_result)
    if list_result.returncode != 0:
        raise FatalError(
            f"UMR --list-uq failed with status {list_result.returncode}; "
            f"see {list_path}"
        )

    queues = parse_queue_list(list_result.output, args.pid)
    after = []  # type: List[MqdQueueIdentity]
    if len(set(process_gpu_ids)) > 1:
        after = capture_kfd_mqd_queue_inventory(
            prefix=prefix,
            pid=args.pid,
            timeout=args.command_timeout,
            path=output_dir / f"snapshot-{index:02d}-kfd-mqds-after.txt",
        )
    queues = map_umr_queues_to_gpu(
        queues=queues,
        target_gpu_id=target_gpu_id,
        process_gpu_ids=process_gpu_ids,
        before=before,
        after=after,
        findings=findings,
        snapshot_index=index,
    )

    if args.queue_ids:
        requested = set(args.queue_ids)
        queues = [queue for queue in queues if queue.queue_id in requested]
    if not queues:
        mapping_errors = [
            f"{finding.code}: {finding.message}"
            for finding in findings
            if finding.snapshot == index and finding.severity == "ERROR"
        ]
        detail = (
            "; ".join(mapping_errors)
            if mapping_errors
            else "the selected UMR-local queue filter matched no queue"
        )
        diagnostic_paths = (
            f"{list_path} and the snapshot KFD MQD mapping logs"
            if len(set(process_gpu_ids)) > 1
            else str(list_path)
        )
        raise FatalError(
            f"no unambiguous UMR queues found for PID {args.pid} on gpu_id "
            f"{target_gpu_id}; no queue was dumped. {detail}. "
            f"See {diagnostic_paths}"
        )

    snapshot = Snapshot(
        index=index,
        timestamp=timestamp,
        list_path=str(list_path),
        state_path=None,
        queues=queues,
    )

    state_path = output_dir / f"snapshot-{index:02d}-queue-state.txt"
    snapshot.state_path = str(state_path)
    state_results = []
    for queue in queues:
        state_result = run_command(
            [
                *base_command,
                "--user-queue",
                f"kfd,pid={args.pid},queue={queue.queue_id}",
                "--print-uq",
            ],
            args.command_timeout,
        )
        state_results.append(state_result)
        if state_result.returncode == 0:
            parsed_states = parse_queue_states(state_result.output)
            matching_states = [
                state
                for state in parsed_states
                if state.queue_id == queue.queue_id
            ]
            if len(matching_states) == 1:
                state = matching_states[0]
                try:
                    queue_mqd = parse_integer(queue.mqd_gpu_address)
                    state_mqd = parse_integer(state.mqd_gpu_address)
                except (TypeError, ValueError):
                    queue_mqd = state_mqd = None
                identity_matches = (
                    (state.queue_type is None
                     or state.queue_type == queue.queue_type)
                    and (
                        queue_mqd is None
                        or state_mqd is None
                        or queue_mqd == state_mqd
                    )
                )
                if identity_matches:
                    snapshot.states.append(state)
                else:
                    findings.append(Finding(
                        "ERROR",
                        "UMR_STATE_IDENTITY_MISMATCH",
                        (
                            "queue-bound --print-uq record does not match "
                            "the selected --list-uq type/MQD address; refusing "
                            "it for pending/full-ring correlation"
                        ),
                        queue.queue_id,
                        index,
                    ))
            else:
                findings.append(Finding(
                    "ERROR",
                    "UMR_STATE_CONTEXT_MISMATCH",
                    (
                        f"queue-bound --print-uq returned "
                        f"{len(matching_states)} state record(s) for the "
                        "selected queue; refusing states for other queue IDs"
                    ),
                    queue.queue_id,
                    index,
                ))
            findings.extend(analyze_mqd_output(state_result.output, index))
        else:
            findings.append(Finding(
                "ERROR", "UMR_STATE_DUMP_FAILED",
                f"UMR --print-uq exited with status {state_result.returncode}",
                queue.queue_id, index,
            ))
    state_path.write_text(
        "\n".join(
            "$ " + " ".join(shlex.quote(str(item)) for item in result.argv)
            + f"\n[started_at={result.started_at or '<unavailable>'}]"
            + f"\n[finished_at={result.finished_at or '<unavailable>'}]"
            + f"\n[exit={result.returncode}]\n{result.output}"
            for result in state_results
        ),
        encoding="utf-8",
    )

    states_by_id = {state.queue_id: state for state in snapshot.states}
    for queue in queues:
        if queue.queue_type != 3:
            continue
        state = states_by_id.get(queue.queue_id)
        if (
            state is None
            or state.sdma_engine_id is None
            or state.sdma_queue_id is None
        ):
            findings.append(
                Finding(
                    "WARNING",
                    "SDMA_MQD_MAPPING_UNAVAILABLE",
                    (
                        "could not read sdma_engine_id/sdma_queue_id from "
                        "the queue MQD; skipping hardware block register dump"
                    ),
                    queue.queue_id,
                    index,
                )
            )
            continue

        block = sdma_blocks.get(state.sdma_engine_id)
        if block is None:
            findings.append(
                Finding(
                    "ERROR",
                    "SDMA_BLOCK_NOT_FOUND",
                    (
                        f"MQD selects SDMA engine {state.sdma_engine_id}, "
                        "but --list-blocks did not report that instance"
                    ),
                    queue.queue_id,
                    index,
                )
            )
            continue

        register_path = (
            f"{block}.regSDMA_RLC{state.sdma_queue_id}_*"
        )
        register_result = run_command(
            [
                *base_command,
                "-O",
                "bits",
                "-r",
                register_path,
            ],
            args.command_timeout,
        )
        register_dump_path = (
            output_dir
            / (
                f"snapshot-{index:02d}-queue-{queue.queue_id}-sdma-"
                f"engine-{state.sdma_engine_id}-rlc-{state.sdma_queue_id}-"
                "registers.txt"
            )
        )
        write_command_log(register_dump_path, register_result)
        snapshot.sdma_register_dumps.append(
            {
                "queue_id": queue.queue_id,
                "engine_id": state.sdma_engine_id,
                "rlc_queue_id": state.sdma_queue_id,
                "block": block,
                "register_path": register_path,
                "path": str(register_dump_path),
                "command_status": register_result.returncode,
            }
        )
        if (
            register_result.returncode != 0
            or "[ERROR]" in register_result.output
        ):
            findings.append(
                Finding(
                    "ERROR",
                    "SDMA_REGISTER_DUMP_FAILED",
                    (
                        f"failed to read {register_path}; "
                        f"see {register_dump_path}"
                    ),
                    queue.queue_id,
                    index,
                )
            )

    for queue in queues:
        state = states_by_id.get(queue.queue_id)
        queue_command, queue_partition, partition_source = queue_vm_command(
            base_command, queue, state
        )
        result = run_command(
            queue_dump_command(queue_command, args.pid, queue, "pending"),
            args.command_timeout,
        )
        dump_path = queue_dump_path(output_dir, index, queue, "pending")
        write_command_log(dump_path, result)
        dump, dump_findings = analyze_packet_dump(
            queue, "pending", dump_path, result, index, args.command_timeout
        )
        snapshot.packet_dumps.append(dump)
        findings.extend(dump_findings)

        if (
            getattr(args, "no_full_ring", False)
            or queue.queue_type not in (1, 2)
        ):
            continue
        try:
            artifact = capture_full_ring(
                base_command=queue_command,
                pid=args.pid,
                queue=queue,
                state=state,
                output_dir=output_dir,
                snapshot_index=index,
                timeout=args.command_timeout,
                findings=findings,
                keep_raw=getattr(args, "keep_full_ring_raw", True),
                pending_result=result,
                vm_partition_source=partition_source,
            ) if state is not None else {
                "queue_id": queue.queue_id,
                "error": "queue state unavailable",
                "vm_partition": queue_partition,
                "xcc_context_source": partition_source,
            }
            snapshot.full_ring_artifacts.append(artifact)
        except (OSError, ValueError, struct.error) as exc:
            findings.append(Finding(
                "WARNING",
                "FULL_RING_CAPTURE_FAILED",
                f"full-ring collector isolated failure: {exc}",
                queue.queue_id,
                index,
            ))
            snapshot.full_ring_artifacts.append({
                "queue_id": queue.queue_id,
                "error": str(exc),
                "vm_partition": queue_partition,
                "xcc_context_source": partition_source,
            })

    findings.extend(analyze_snapshot_state(snapshot))
    return snapshot


def queue_dump_command(
    base_command: Sequence[str], pid: int, queue: QueueInfo, mode: str
) -> List[str]:
    extra_options = ["-O", "use_full_user_queue"] if mode == "full-ring" else []
    return [
        *base_command,
        *extra_options,
        "--user-queue",
        f"kfd,pid={pid},queue={queue.queue_id}",
        "--dump-uq",
    ]


def queue_dump_path(
    output_dir: Path, snapshot_index: int, queue: QueueInfo, mode: str
) -> Path:
    directory = output_dir / ("full-ring" if mode == "full-ring" else "pending")
    return directory / (
        f"snapshot-{snapshot_index:02d}-queue-{queue.queue_id}-"
        f"{queue.queue_type_name}.txt"
    )


def full_ring_artifact_summary_lines(
    snapshots: Sequence[Any], indent: str = "  "
) -> List[str]:
    lines = []  # type: List[str]
    for snapshot in snapshots:
        if isinstance(snapshot, dict):
            snapshot_index = snapshot.get("index", "<unknown>")
            artifacts = snapshot.get("full_ring_artifacts", [])
        else:
            snapshot_index = snapshot.index
            artifacts = snapshot.full_ring_artifacts
        for artifact in artifacts:
            raw_path = artifact.get("raw_path")
            if raw_path is None:
                raw_path = artifact.get("raw_capture_path", "<unavailable>")
                if artifact.get("decode_complete"):
                    raw_path += " (discarded after successful decode)"
                elif raw_path != "<unavailable>":
                    raw_path += " (no raw data retained)"
            decoded_path = artifact.get("decoded_path", "<unavailable>")
            lines.append(
                f"{indent}snapshot={snapshot_index} "
                f"queue={artifact.get('queue_id', '<unknown>')} "
                f"raw={raw_path} decoded={decoded_path}"
            )
    return lines or [f"{indent}<none>"]


def render_summary(
    *,
    args: argparse.Namespace,
    node: NodeInfo,
    umr: Path,
    process_name: str,
    output_dir: Path,
    snapshots: Sequence[Snapshot],
    findings: Sequence[Finding],
    sdma_blocks: Optional[Dict[int, str]] = None,
    kfd_queues: Optional[Sequence[KfdQueueInfo]] = None,
) -> str:
    gpu_kfd_queues = queues_for_gpu(kfd_queues or [], node.gpu_id)
    kfd_queue_ids = ", ".join(
        str(queue.queue_id) for queue in gpu_kfd_queues
    )
    lines = [
        "UMR Queue Doctor",
        "================",
        f"Process: PID {args.pid} ({process_name})",
        (
            f"GPU: AMD-SMI={node.amd_smi_id if node.amd_smi_id is not None else 'unknown'}, "
            f"KFD topology node={node.node_id}, KFD gpu_id={node.gpu_id}, "
            f"PCI={node.pci_bdf}, renderD{node.render_minor}"
        ),
        (
            "Target source: "
            f"{KFD_PROCESS_ROOT}/{args.pid}/queues/*/gpuid -> KFD topology -> "
            "PCI/render/AMD-SMI"
        ),
        (
            f"KFD sysfs queues on this GPU: count={len(gpu_kfd_queues)}, "
            f"IDs=[{kfd_queue_ids}]"
        ),
        (
            "UMR mapping: queue= is a separate 0-based index in "
            f"{KFD_MQDS_PATH}; multi-GPU mapping uses its PID-scoped ordered "
            "device gpu_id headers bracketed around --list-uq"
        ),
        f"UMR: {umr}",
        (
            "SDMA blocks: "
            + (
                ", ".join(
                    f"engine {engine}={block}"
                    for engine, block in sorted((sdma_blocks or {}).items())
                )
                if sdma_blocks
                else "unavailable"
            )
        ),
        f"Snapshots: {len(snapshots)}, interval={args.interval}s",
        "Full-ring raw retention policy: "
        + full_ring_raw_retention_policy(
            getattr(args, "keep_full_ring_raw", True)
        ),
        f"Artifacts: {output_dir}",
        "",
    ]
    for snapshot in snapshots:
        lines.append(f"Snapshot {snapshot.index} @ {snapshot.timestamp}")
        for queue in snapshot.queues:
            state = "pending" if queue.pending else "idle"
            lines.append(
                f"  umr_queue={queue.queue_id} type={queue.queue_type_name}"
                f"({queue.queue_type}) rptr=0x{queue.rptr:x} "
                f"wptr=0x{queue.wptr:x} state={state} "
                f"kfd_gpu_id={queue.kfd_gpu_id}"
            )
        lines.append("")

    severity_counts = {
        severity: sum(1 for finding in findings if finding.severity == severity)
        for severity in ("ERROR", "WARNING", "INFO")
    }
    lines.append(
        "Dump counts: "
        f"pending={packet_dump_counts(snapshots)['pending_success']}/"
        f"{packet_dump_counts(snapshots)['pending_total']} "
        f"full-ring={packet_dump_counts(snapshots)['full_ring_success']}/"
        f"{packet_dump_counts(snapshots)['full_ring_total']}"
    )
    lines.append("Full-ring artifacts:")
    lines.extend(full_ring_artifact_summary_lines(snapshots))
    lines.append(
        "Findings: "
        f"errors={severity_counts['ERROR']} "
        f"warnings={severity_counts['WARNING']} "
        f"info={severity_counts['INFO']}"
    )
    if findings:
        for finding in findings:
            location = []
            if finding.snapshot is not None:
                location.append(f"snapshot={finding.snapshot}")
            if finding.queue_id is not None:
                location.append(f"queue={finding.queue_id}")
            suffix = f" ({', '.join(location)})" if location else ""
            lines.append(
                f"  [{finding.severity}] {finding.code}{suffix}: {finding.message}"
            )
    else:
        lines.append("  No anomalies detected.")
    return "\n".join(lines) + "\n"


def finding_counts(findings: Sequence[Finding]) -> Dict[str, int]:
    return {
        "errors": sum(1 for item in findings if item.severity == "ERROR"),
        "warnings": sum(1 for item in findings if item.severity == "WARNING"),
        "info": sum(1 for item in findings if item.severity == "INFO"),
    }


def packet_dump_counts(snapshots: Sequence[Snapshot]) -> Dict[str, int]:
    counts = {
        "pending_total": 0,
        "pending_success": 0,
        "full_ring_total": 0,
        "full_ring_success": 0,
    }
    for snapshot in snapshots:
        for dump in snapshot.packet_dumps:
            prefix = "full_ring" if dump.mode == "full-ring" else "pending"
            counts[f"{prefix}_total"] += 1
            if dump.command_status == 0:
                counts[f"{prefix}_success"] += 1
        for artifact in snapshot.full_ring_artifacts:
            counts["full_ring_total"] += 1
            if artifact.get("decode_complete"):
                counts["full_ring_success"] += 1
    return counts


def capture_node_report(
    *,
    args: argparse.Namespace,
    node: NodeInfo,
    kfd_queues: Sequence[KfdQueueInfo],
    umr: Path,
    prefix: Sequence[str],
    process_name: str,
    output_dir: Path,
) -> Tuple[Dict[str, Any], str]:
    (output_dir / "pending").mkdir(parents=True, exist_ok=True)
    if not getattr(args, "no_full_ring", False):
        (output_dir / "full-ring").mkdir(parents=True, exist_ok=True)
    base_command = base_umr_command(prefix, umr, node, args.vm_partition)
    sdma_blocks, block_list_path = discover_sdma_blocks(
        base_command,
        output_dir,
        args.command_timeout,
    )
    findings = []  # type: List[Finding]
    snapshots = []  # type: List[Snapshot]
    for index in range(args.samples):
        snapshot = capture_snapshot(
            index=index,
            args=args,
            target_gpu_id=node.gpu_id,
            process_gpu_ids=[queue.gpu_id for queue in kfd_queues],
            prefix=prefix,
            output_dir=output_dir,
            base_command=base_command,
            sdma_blocks=sdma_blocks,
            findings=findings,
        )
        snapshots.append(snapshot)
        if index + 1 < args.samples and args.interval:
            time.sleep(args.interval)

    findings.extend(analyze_progress(snapshots))
    summary = render_summary(
        args=args,
        node=node,
        umr=umr,
        process_name=process_name,
        output_dir=output_dir,
        snapshots=snapshots,
        findings=findings,
        sdma_blocks=sdma_blocks,
        kfd_queues=kfd_queues,
    )
    queue_gpu_ids = {
        queue.queue_id: queue.gpu_id for queue in kfd_queues
    }
    report = {  # type: Dict[str, Any]
        "schema_version": 2,
        "process": {"pid": args.pid, "name": process_name},
        "gpu": vars(node),
        "queue_gpu_ids": queue_gpu_ids,
        "kfd_queues": [record_to_dict(queue) for queue in kfd_queues],
        "kfd_queue_ids_on_gpu": [
            queue.queue_id
            for queue in queues_for_gpu(kfd_queues, node.gpu_id)
        ],
        "queue_namespace": {
            "kfd_sysfs": "process-global queue directory ID",
            "umr": f"0-based PID MQD order from {KFD_MQDS_PATH}",
            "join": "ordered MQD device gpu_id headers, never numeric ID equality",
        },
        "umr": str(umr),
        "vm_partition": args.vm_partition,
        "full_ring_raw_retention": {
            "keep_after_success": getattr(args, "keep_full_ring_raw", True),
            "policy": full_ring_raw_retention_policy(
                getattr(args, "keep_full_ring_raw", True)
            ),
        },
        "output_dir": str(output_dir),
        "block_list_path": str(block_list_path),
        "sdma_blocks": sdma_blocks,
        "snapshots": [record_to_dict(snapshot) for snapshot in snapshots],
        "findings": [record_to_dict(finding) for finding in findings],
        "summary": finding_counts(findings),
        "dump_counts": packet_dump_counts(snapshots),
        "dump_directories": {
            "pending": str(output_dir / "pending"),
            "full-ring": str(output_dir / "full-ring"),
        },
    }
    (output_dir / "summary.txt").write_text(summary, encoding="utf-8")
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report, summary


def failed_node_report(
    args: argparse.Namespace,
    node: NodeInfo,
    kfd_queues: Sequence[KfdQueueInfo],
    process_name: str,
    umr: Path,
    output_dir: Path,
    message: str,
) -> Tuple[Dict[str, Any], str]:
    finding = Finding("ERROR", "GPU_CAPTURE_FAILED", message)
    summary = render_summary(
        args=args,
        node=node,
        umr=umr,
        process_name=process_name,
        output_dir=output_dir,
        snapshots=[],
        findings=[finding],
        kfd_queues=kfd_queues,
    )
    report = {  # type: Dict[str, Any]
        "schema_version": 2,
        "process": {"pid": args.pid, "name": process_name},
        "gpu": vars(node),
        "kfd_queues": [record_to_dict(queue) for queue in kfd_queues],
        "kfd_queue_ids_on_gpu": [
            queue.queue_id
            for queue in queues_for_gpu(kfd_queues, node.gpu_id)
        ],
        "umr": str(umr),
        "vm_partition": args.vm_partition,
        "full_ring_raw_retention": {
            "keep_after_success": getattr(args, "keep_full_ring_raw", True),
            "policy": full_ring_raw_retention_policy(
                getattr(args, "keep_full_ring_raw", True)
            ),
        },
        "output_dir": str(output_dir),
        "snapshots": [],
        "findings": [record_to_dict(finding)],
        "summary": finding_counts([finding]),
    }
    (output_dir / "summary.txt").write_text(summary, encoding="utf-8")
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report, summary


def render_multi_gpu_summary(
    args: argparse.Namespace,
    process_name: str,
    output_dir: Path,
    device_reports: Sequence[Dict[str, Any]],
    inventory: GpuInventory,
) -> str:
    totals = {
        key: sum(report["summary"][key] for report in device_reports)
        for key in ("errors", "warnings", "info")
    }
    lines = render_target_gpu_inventory(args.pid, inventory).rstrip().splitlines()
    lines.extend(
        [
        "",
        "UMR Queue Doctor - Multi-GPU",
        "============================",
        f"Process: PID {args.pid} ({process_name})",
        f"GPUs captured sequentially: {len(device_reports)}",
        "Full-ring raw retention policy: "
        + full_ring_raw_retention_policy(
            getattr(args, "keep_full_ring_raw", True)
        ),
        f"Artifacts: {output_dir}",
        "",
        ]
    )
    for report in device_reports:
        gpu = report["gpu"]
        counts = report["summary"]
        queue_count = sum(
            len(snapshot["queues"]) for snapshot in report["snapshots"][:1]
        )
        lines.append(
            f"  amd_smi={gpu['amd_smi_id']} node={gpu['node_id']} "
            f"kfd_gpu_id={gpu['gpu_id']} "
            f"PCI={gpu['pci_bdf']} kfd_queues="
            f"{len(report.get('kfd_queue_ids_on_gpu', []))} "
            f"captured_umr_queues={queue_count} "
            f"errors={counts['errors']} warnings={counts['warnings']} "
            f"info={counts['info']}"
        )
        lines.append(f"    {report['output_dir']}")
        dump_counts = report.get("dump_counts", {})
        lines.append(
            f"    pending={dump_counts.get('pending_success', 0)}/"
            f"{dump_counts.get('pending_total', 0)} "
            f"full-ring={dump_counts.get('full_ring_success', 0)}/"
            f"{dump_counts.get('full_ring_total', 0)}"
        )
        lines.append("    full-ring artifacts:")
        lines.extend(
            full_ring_artifact_summary_lines(
                report.get("snapshots", []), indent="      "
            )
        )
    lines.extend(
        [
            "",
            f"Total: errors={totals['errors']} warnings={totals['warnings']} "
            f"info={totals['info']}",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    try:
        selection_source = "explicit-pid"
        discovered_amd_smi_gpu_ids = []  # type: List[int]
        discovery_output = None  # type: Optional[str]
        if args.pid is None:
            (
                args.pid,
                discovered_amd_smi_gpu_ids,
                discovery_output,
            ) = discover_process_from_amd_smi(
                args.process_name, args.command_timeout
            )
            selection_source = "amd-smi"
            print(
                f"Auto-selected PID {args.pid} for {args.process_name!r}; "
                "AMD-SMI GPU(s): "
                + ", ".join(
                    str(gpu_id) for gpu_id in discovered_amd_smi_gpu_ids
                )
            )

        process_path = Path("/proc") / str(args.pid)
        if not process_path.is_dir():
            raise FatalError(f"PID {args.pid} is not running in the host namespace")
        if selection_source == "amd-smi":
            process_name = args.process_name
        else:
            process_name = (
                (process_path / "comm").read_text(encoding="utf-8").strip()
            )
        kfd_queues = read_process_queues(args.pid)
        queue_gpu_ids = {
            queue.queue_id: queue.gpu_id for queue in kfd_queues
        }
        inventory = discover_gpu_inventory(args, kfd_queues)
        nodes = inventory.selected_nodes
        inventory_summary = render_target_gpu_inventory(args.pid, inventory)
        if args.inventory_only:
            print(inventory_summary, end="")
            return 0

        output_dir = make_output_dir(args, nodes)
        inventory_data = inventory_to_dict(args.pid, inventory)
        (output_dir / "target-gpus.json").write_text(
            json.dumps(inventory_data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        # Leave a useful summary on disk before UMR/sudo/GPU capture can fail.
        (output_dir / "summary.txt").write_text(
            inventory_summary
            + "\nQueue Doctor status: target inventory complete; capture pending.\n",
            encoding="utf-8",
        )
        if discovery_output is not None:
            (output_dir / "amd-smi-process-discovery.txt").write_text(
                discovery_output, encoding="utf-8"
            )
        process_selection = {
            "source": selection_source,
            "requested_process_name": args.process_name,
            "pid": args.pid,
            "amd_smi_gpu_ids": discovered_amd_smi_gpu_ids,
            "kfd_queue_gpu_ids": sorted(set(queue_gpu_ids.values())),
            "kfd_queues": [record_to_dict(queue) for queue in kfd_queues],
            "selected_nodes": [vars(node) for node in nodes],
        }
        (output_dir / "process-selection.json").write_text(
            json.dumps(process_selection, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        try:
            umr = resolve_umr(args.umr_bin)
            prefix = command_prefix(args.no_sudo)
        except (FatalError, OSError) as exc:
            setup_summary = (
                inventory_summary
                + "\nQueue Doctor setup failed before any GPU dump:\n"
                + f"  [ERROR] {exc}\n"
                + "Full-ring raw retention policy: "
                + full_ring_raw_retention_policy(args.keep_full_ring_raw)
                + "\n"
                + f"Artifacts: {output_dir}\n"
            )
            setup_report = {
                "schema_version": 3,
                "process": {"pid": args.pid, "name": process_name},
                "target_gpu_inventory": inventory_data,
                "full_ring_raw_retention": {
                    "keep_after_success": args.keep_full_ring_raw,
                    "policy": full_ring_raw_retention_policy(
                        args.keep_full_ring_raw
                    ),
                },
                "output_dir": str(output_dir),
                "capture_started": False,
                "error": str(exc),
            }
            (output_dir / "summary.txt").write_text(
                setup_summary, encoding="utf-8"
            )
            (output_dir / "report.json").write_text(
                json.dumps(setup_report, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

        device_reports = []  # type: List[Dict[str, Any]]
        for node in nodes:
            if len(nodes) == 1:
                node_output_dir = output_dir
            else:
                if node.amd_smi_id is not None:
                    device_name = (
                        f"gpu-{node.amd_smi_id}-node-{node.node_id}-"
                        f"kfd-{node.gpu_id}"
                    )
                else:
                    device_name = f"node-{node.node_id}-kfd-{node.gpu_id}"
                node_output_dir = output_dir / device_name
                node_output_dir.mkdir(parents=False, exist_ok=False)

            try:
                report, summary = capture_node_report(
                    args=args,
                    node=node,
                    kfd_queues=kfd_queues,
                    umr=umr,
                    prefix=prefix,
                    process_name=process_name,
                    output_dir=node_output_dir,
                )
            except (FatalError, OSError) as exc:
                report, summary = failed_node_report(
                    args,
                    node,
                    kfd_queues,
                    process_name,
                    umr,
                    node_output_dir,
                    str(exc),
                )
            device_reports.append(report)
            print(summary, end="")

        if len(nodes) > 1:
            aggregate_counts = {
                key: sum(report["summary"][key] for report in device_reports)
                for key in ("errors", "warnings", "info")
            }
            aggregate_summary = render_multi_gpu_summary(
                args, process_name, output_dir, device_reports, inventory
            )
            aggregate_report = {
                "schema_version": 3,
                "process": {"pid": args.pid, "name": process_name},
                "queue_gpu_ids": queue_gpu_ids,
                "target_gpu_inventory": inventory_data,
                "umr": str(umr),
                "vm_partition": args.vm_partition,
                "full_ring_raw_retention": {
                    "keep_after_success": args.keep_full_ring_raw,
                    "policy": full_ring_raw_retention_policy(
                        args.keep_full_ring_raw
                    ),
                },
                "output_dir": str(output_dir),
                "devices": device_reports,
                "summary": aggregate_counts,
            }
            (output_dir / "summary.txt").write_text(
                aggregate_summary, encoding="utf-8"
            )
            (output_dir / "report.json").write_text(
                json.dumps(aggregate_report, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(
                without_target_gpu_inventory(
                    aggregate_summary, inventory_summary
                ),
                end="",
            )
        else:
            single_summary = inventory_summary + "\n" + summary
            report["schema_version"] = 3
            report["target_gpu_inventory"] = inventory_data
            (output_dir / "summary.txt").write_text(
                single_summary, encoding="utf-8"
            )
            (output_dir / "report.json").write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

        total_errors = sum(
            report["summary"]["errors"] for report in device_reports
        )
        total_warnings = sum(
            report["summary"]["warnings"] for report in device_reports
        )
        if total_errors:
            return 2
        if args.fail_on_warning and total_warnings:
            return 2
        return 0
    except (FatalError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
