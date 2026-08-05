#!/usr/bin/env python3
"""Discover, dump, and analyze one process's UMR user queues.

Author: Perry_Yuan@outlook.com
Log History:
    2026-08-03 - Added automatic process/GPU discovery and queue diagnostics.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


TOPOLOGY_ROOT = Path("/sys/class/kfd/kfd/topology/nodes")
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
        self.hqd_rptr_address = None
        self.hqd_rptr_value = None
        self.wptr_poll_address = None
        self.wptr_poll_value = None
        self.hqd_active = None
        self.ring_size_bytes = None
        self.sdma_engine_id = None
        self.sdma_queue_id = None


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
    ):
        self.index = index
        self.timestamp = timestamp
        self.list_path = list_path
        self.state_path = state_path
        self.queues = queues or []
        self.states = states or []
        self.packet_dumps = packet_dumps or []
        self.sdma_register_dumps = sdma_register_dumps or []


class Finding:
    def __init__(self, severity, code, message, queue_id=None, snapshot=None):
        self.severity = severity
        self.code = code
        self.message = message
        self.queue_id = queue_id
        self.snapshot = snapshot


class CommandResult:
    def __init__(self, argv, returncode, output):
        self.argv = argv
        self.returncode = returncode
        self.output = output


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
            "progress anomalies."
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
        help="also decode the entire retained ring; output can be very large",
    )
    parser.add_argument(
        "--queue",
        dest="queue_ids",
        type=int,
        action="append",
        help="only dump this queue ID; may be specified multiple times",
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


def read_process_queue_gpu_ids(pid: int) -> Dict[int, int]:
    queue_root = Path("/sys/class/kfd/kfd/proc") / str(pid) / "queues"
    if not queue_root.is_dir():
        return {}

    queue_gpu_ids = {}  # type: Dict[int, int]
    for queue_path in queue_root.iterdir():
        if not queue_path.name.isdigit():
            continue
        gpu_id_path = queue_path / "gpuid"
        if not gpu_id_path.is_file():
            continue
        try:
            queue_gpu_ids[int(queue_path.name)] = int(
                gpu_id_path.read_text(encoding="utf-8").strip(), 0
            )
        except (OSError, ValueError):
            continue
    return queue_gpu_ids


def resolve_target_nodes(
    args: argparse.Namespace, queue_gpu_ids: Dict[int, int]
) -> List[NodeInfo]:
    if args.all_gpus:
        target_gpu_ids = sorted(set(queue_gpu_ids.values()))
        if not target_gpu_ids:
            raise FatalError(
                f"PID {args.pid} has no readable KFD per-queue GPU metadata"
            )

        nodes_by_gpu_id = {}  # type: Dict[int, NodeInfo]
        if TOPOLOGY_ROOT.is_dir():
            for node_path in TOPOLOGY_ROOT.iterdir():
                if not node_path.name.isdigit():
                    continue
                try:
                    node = resolve_node(int(node_path.name))
                except FatalError:
                    continue
                nodes_by_gpu_id[node.gpu_id] = node

        missing_gpu_ids = [
            gpu_id for gpu_id in target_gpu_ids if gpu_id not in nodes_by_gpu_id
        ]
        if missing_gpu_ids:
            raise FatalError(
                "could not map KFD gpu_id(s) to topology nodes: "
                + ", ".join(str(gpu_id) for gpu_id in missing_gpu_ids)
            )
        nodes = [nodes_by_gpu_id[gpu_id] for gpu_id in target_gpu_ids]
        try:
            amd_smi_gpu_bdfs = read_amd_smi_gpu_bdfs(args.command_timeout)
        except FatalError:
            amd_smi_gpu_bdfs = {}
        amd_smi_id_by_bdf = {
            pci_bdf: gpu_id for gpu_id, pci_bdf in amd_smi_gpu_bdfs.items()
        }
        for node in nodes:
            node.amd_smi_id = amd_smi_id_by_bdf.get(node.pci_bdf.lower())
        return nodes

    if args.gpu_ids:
        amd_smi_gpu_bdfs = read_amd_smi_gpu_bdfs(args.command_timeout)
        nodes_by_bdf = {}  # type: Dict[str, NodeInfo]
        if TOPOLOGY_ROOT.is_dir():
            for node_path in TOPOLOGY_ROOT.iterdir():
                if not node_path.name.isdigit():
                    continue
                try:
                    node = resolve_node(int(node_path.name))
                except FatalError:
                    continue
                nodes_by_bdf[node.pci_bdf.lower()] = node

        nodes = []  # type: List[NodeInfo]
        seen_gpu_ids = set()
        for gpu_id in args.gpu_ids:
            if gpu_id in seen_gpu_ids:
                continue
            seen_gpu_ids.add(gpu_id)
            if gpu_id not in amd_smi_gpu_bdfs:
                available = ", ".join(
                    str(item) for item in sorted(amd_smi_gpu_bdfs)
                )
                raise FatalError(
                    f"AMD-SMI GPU {gpu_id} does not exist; available IDs: "
                    f"{available}"
                )
            pci_bdf = amd_smi_gpu_bdfs[gpu_id]
            if pci_bdf not in nodes_by_bdf:
                raise FatalError(
                    f"AMD-SMI GPU {gpu_id} at PCI {pci_bdf} has no matching "
                    "KFD topology GPU node"
                )
            node = nodes_by_bdf[pci_bdf]
            node.amd_smi_id = gpu_id
            nodes.append(node)
        return nodes

    nodes = []  # type: List[NodeInfo]
    seen_node_ids = set()
    for node_id in args.node_ids:
        if node_id in seen_node_ids:
            continue
        seen_node_ids.add(node_id)
        nodes.append(resolve_node(node_id))
    return nodes


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
        return CommandResult(list(argv), 124, output + "\n[tool] command timed out\n")
    return CommandResult(list(argv), completed.returncode, completed.stdout)


def write_command_log(path: Path, result: CommandResult) -> None:
    command = " ".join(result.argv)
    path.write_text(
        f"$ {command}\n[exit={result.returncode}]\n{result.output}",
        encoding="utf-8",
    )


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
        "ring_size_bytes": re.compile(r"^\s*rb_buf_size:\s*(0x[0-9a-fA-F]+)"),
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
            if name in {"hqd_active", "ring_size_bytes"}:
                setattr(current, name, int(value, 16))
            else:
                setattr(current, name, value)
            break
        else:
            rptr_match = pointer_fields["hqd_rptr"].match(line)
            if rptr_match:
                current.hqd_rptr_address = rptr_match.group(1)
                current.hqd_rptr_value = int(rptr_match.group(2), 16)
                continue
            wptr_match = pointer_fields["wptr_poll"].match(line)
            if wptr_match:
                current.wptr_poll_address = wptr_match.group(1)
                current.wptr_poll_value = int(wptr_match.group(2), 16)
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


def analyze_packet_dump(
    queue: QueueInfo,
    mode: str,
    path: Path,
    result: CommandResult,
    snapshot_index: int,
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
    path = output_dir / "list-blocks.log"
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
    queue_gpu_ids: Dict[int, int],
    output_dir: Path,
    base_command: Sequence[str],
    sdma_blocks: Dict[int, str],
    findings: List[Finding],
) -> Snapshot:
    timestamp = datetime.now(timezone.utc).isoformat()
    list_result = run_command([*base_command, "--list-uq"], args.command_timeout)
    list_path = output_dir / f"snapshot-{index:02d}-list-uq.log"
    write_command_log(list_path, list_result)
    if list_result.returncode != 0:
        raise FatalError(
            f"UMR --list-uq failed with status {list_result.returncode}; "
            f"see {list_path}"
        )

    queues = parse_queue_list(list_result.output, args.pid)
    if queue_gpu_ids:
        process_gpu_ids = set(queue_gpu_ids.values())
        if target_gpu_id not in process_gpu_ids:
            raise FatalError(
                f"PID {args.pid} has no KFD queue on gpu_id {target_gpu_id}"
            )

        missing_ids = [
            queue.queue_id
            for queue in queues
            if queue.queue_id not in queue_gpu_ids
        ]
        if missing_ids and len(process_gpu_ids) > 1:
            raise FatalError(
                "UMR queue IDs cannot be matched to KFD sysfs queue IDs in a "
                "multi-GPU process; refusing to dump an ambiguous GPU queue set"
            )
        if not missing_ids:
            other_gpu_queues = [
                queue
                for queue in queues
                if queue_gpu_ids[queue.queue_id] != target_gpu_id
            ]
            queues = [
                queue
                for queue in queues
                if queue_gpu_ids[queue.queue_id] == target_gpu_id
            ]
            if index == 0:
                for queue in other_gpu_queues:
                    findings.append(
                        Finding(
                            "INFO",
                            "QUEUE_ON_OTHER_GPU",
                            (
                                f"excluded queue mapped to gpu_id "
                                f"{queue_gpu_ids[queue.queue_id]}"
                            ),
                            queue.queue_id,
                            index,
                        )
                    )
    elif index == 0:
        findings.append(
            Finding(
                "WARNING",
                "GPU_QUEUE_FILTER_UNAVAILABLE",
                (
                    "KFD per-queue gpuid metadata is unavailable; UMR's KFD "
                    "enumerator may not filter queues by the selected GPU"
                ),
                snapshot=index,
            )
        )

    if args.queue_ids:
        requested = set(args.queue_ids)
        queues = [queue for queue in queues if queue.queue_id in requested]
    if not queues:
        raise FatalError(
            f"no queues found for PID {args.pid} on the selected GPU; see {list_path}"
        )

    snapshot = Snapshot(
        index=index,
        timestamp=timestamp,
        list_path=str(list_path),
        state_path=None,
        queues=queues,
    )

    first_queue = queues[0]
    state_result = run_command(
        [
            *base_command,
            "--user-queue",
            f"kfd,pid={args.pid},queue={first_queue.queue_id}",
            "--print-uq",
        ],
        args.command_timeout,
    )
    state_path = output_dir / f"snapshot-{index:02d}-queue-state.log"
    write_command_log(state_path, state_result)
    snapshot.state_path = str(state_path)
    if state_result.returncode == 0:
        snapshot.states = parse_queue_states(state_result.output)
        findings.extend(analyze_mqd_output(state_result.output, index))
    else:
        findings.append(
            Finding(
                "ERROR",
                "UMR_STATE_DUMP_FAILED",
                f"UMR --print-uq exited with status {state_result.returncode}",
                snapshot=index,
            )
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
                "registers.log"
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
        modes = ["pending"]
        if args.full_ring:
            modes.append("full")
        for mode in modes:
            extra_options = ["-O", "use_full_user_queue"] if mode == "full" else []
            result = run_command(
                [
                    *base_command,
                    *extra_options,
                    "--user-queue",
                    f"kfd,pid={args.pid},queue={queue.queue_id}",
                    "--dump-uq",
                ],
                args.command_timeout,
            )
            dump_path = (
                output_dir
                / f"snapshot-{index:02d}-queue-{queue.queue_id}-{queue.queue_type_name}-{mode}.log"
            )
            write_command_log(dump_path, result)
            dump, dump_findings = analyze_packet_dump(
                queue, mode, dump_path, result, index
            )
            snapshot.packet_dumps.append(dump)
            findings.extend(dump_findings)

    findings.extend(analyze_snapshot_state(snapshot))
    return snapshot


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
) -> str:
    lines = [
        "UMR Queue Doctor",
        "================",
        f"Process: PID {args.pid} ({process_name})",
        (
            f"GPU: AMD-SMI={node.amd_smi_id if node.amd_smi_id is not None else 'unknown'}, "
            f"KFD topology node={node.node_id}, KFD gpu_id={node.gpu_id}, "
            f"PCI={node.pci_bdf}, renderD{node.render_minor}"
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
        f"Artifacts: {output_dir}",
        "",
    ]
    for snapshot in snapshots:
        lines.append(f"Snapshot {snapshot.index} @ {snapshot.timestamp}")
        for queue in snapshot.queues:
            state = "pending" if queue.pending else "idle"
            lines.append(
                f"  queue={queue.queue_id} type={queue.queue_type_name}"
                f"({queue.queue_type}) rptr=0x{queue.rptr:x} "
                f"wptr=0x{queue.wptr:x} state={state}"
            )
        lines.append("")

    severity_counts = {
        severity: sum(1 for finding in findings if finding.severity == severity)
        for severity in ("ERROR", "WARNING", "INFO")
    }
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


def capture_node_report(
    *,
    args: argparse.Namespace,
    node: NodeInfo,
    queue_gpu_ids: Dict[int, int],
    umr: Path,
    prefix: Sequence[str],
    process_name: str,
    output_dir: Path,
) -> Tuple[Dict[str, Any], str]:
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
            queue_gpu_ids=queue_gpu_ids,
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
    )
    report = {  # type: Dict[str, Any]
        "schema_version": 1,
        "process": {"pid": args.pid, "name": process_name},
        "gpu": vars(node),
        "queue_gpu_ids": queue_gpu_ids,
        "umr": str(umr),
        "vm_partition": args.vm_partition,
        "output_dir": str(output_dir),
        "block_list_path": str(block_list_path),
        "sdma_blocks": sdma_blocks,
        "snapshots": [record_to_dict(snapshot) for snapshot in snapshots],
        "findings": [record_to_dict(finding) for finding in findings],
        "summary": finding_counts(findings),
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
    )
    report = {  # type: Dict[str, Any]
        "schema_version": 1,
        "process": {"pid": args.pid, "name": process_name},
        "gpu": vars(node),
        "umr": str(umr),
        "vm_partition": args.vm_partition,
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
) -> str:
    totals = {
        key: sum(report["summary"][key] for report in device_reports)
        for key in ("errors", "warnings", "info")
    }
    lines = [
        "UMR Queue Doctor - Multi-GPU",
        "============================",
        f"Process: PID {args.pid} ({process_name})",
        f"GPUs captured sequentially: {len(device_reports)}",
        f"Artifacts: {output_dir}",
        "",
    ]
    for report in device_reports:
        gpu = report["gpu"]
        counts = report["summary"]
        queue_count = sum(
            len(snapshot["queues"]) for snapshot in report["snapshots"][:1]
        )
        lines.append(
            f"  amd_smi={gpu['amd_smi_id']} node={gpu['node_id']} "
            f"kfd_gpu_id={gpu['gpu_id']} "
            f"PCI={gpu['pci_bdf']} queues={queue_count} "
            f"errors={counts['errors']} warnings={counts['warnings']} "
            f"info={counts['info']}"
        )
        lines.append(f"    {report['output_dir']}")
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
        queue_gpu_ids = read_process_queue_gpu_ids(args.pid)
        nodes = resolve_target_nodes(args, queue_gpu_ids)
        umr = resolve_umr(args.umr_bin)
        prefix = command_prefix(args.no_sudo)
        output_dir = make_output_dir(args, nodes)
        if discovery_output is not None:
            (output_dir / "amd-smi-process-discovery.log").write_text(
                discovery_output, encoding="utf-8"
            )
        process_selection = {
            "source": selection_source,
            "requested_process_name": args.process_name,
            "pid": args.pid,
            "amd_smi_gpu_ids": discovered_amd_smi_gpu_ids,
            "kfd_queue_gpu_ids": sorted(set(queue_gpu_ids.values())),
            "selected_nodes": [vars(node) for node in nodes],
        }
        (output_dir / "process-selection.json").write_text(
            json.dumps(process_selection, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

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
                    queue_gpu_ids=queue_gpu_ids,
                    umr=umr,
                    prefix=prefix,
                    process_name=process_name,
                    output_dir=node_output_dir,
                )
            except (FatalError, OSError) as exc:
                report, summary = failed_node_report(
                    args,
                    node,
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
                args, process_name, output_dir, device_reports
            )
            aggregate_report = {
                "schema_version": 2,
                "process": {"pid": args.pid, "name": process_name},
                "queue_gpu_ids": queue_gpu_ids,
                "umr": str(umr),
                "vm_partition": args.vm_partition,
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
            print(aggregate_summary, end="")

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
