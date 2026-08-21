#!/usr/bin/env python3
"""Parse standalone UMR ``--tool cpc`` text captures.

The parser deliberately keeps parsing and decoding separate.  Text structure
and raw values are retained for every ASIC.  Bit fields are decoded only when
an explicitly selected or independently identified profile is available.
"""

from __future__ import print_function

import argparse
import json
import os
import re
import shlex
import sys


SCHEMA_NAME = "umr-cpc-report"
SCHEMA_VERSION = "1.0"
TOOL_VERSION = "1.0.0"

EXIT_OK = 0
EXIT_INPUT_ERROR = 1
EXIT_NO_MQD_MATCH = 3
EXIT_MULTIPLE_MQD_MATCHES = 4

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
QUEUE_RE = re.compile(
    r"^\s*me(?P<me>\d+)\s*\.\s*pipe(?P<pipe>\d+)\s*\.\s*"
    r"queue(?P<queue>\d+)\s*$",
    re.IGNORECASE,
)
PIPE_RE = re.compile(
    r"^\s*me(?P<me>\d+)\s*\.\s*pipe(?P<pipe>\d+)\s*$",
    re.IGNORECASE,
)
SECTION_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 /_-]*?)\s*:\s*$")
REGISTER_RE = re.compile(
    r"(?<![0-9A-Za-z_])(?P<name>[A-Za-z][A-Za-z0-9_]*)\s*:\s*"
    r"(?P<raw>(?:0x)?[0-9A-Fa-f]+)"
)
PENDING_REGISTER_RE = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9_]*)\s*:\s*$"
)
MQD_ROW_RE = re.compile(
    r"^\s*0x(?P<index>[0-9A-Fa-f]+)\s*:\s*(?P<body>.*)$"
)
BARE_HEX_RE = re.compile(r"^(?:0x)?[0-9A-Fa-f]+$")
FILENAME_CONTEXT_RE = re.compile(
    r"(?:^|_)gpu(?P<gpu>\d+)(?:_xcc(?P<xcc>\d+))?(?:_|\.|$)",
    re.IGNORECASE,
)
COMMAND_INSTANCE_RE = re.compile(r"(?:^|\s)-i\s+(?P<instance>\d+)(?:\s|$)")
COMMAND_VMP_RE = re.compile(r"(?:^|\s)-vmp\s+(?P<vmp>-?\d+)(?:\s|$)")
KERNEL_MINOR_RE = re.compile(
    r"Initialized\s+amdgpu\b.*?\bfor\s+"
    r"(?P<bdf>[0-9A-Fa-f]{4}:[0-9A-Fa-f]{2}:"
    r"[0-9A-Fa-f]{2}\.[0-7])\s+on\s+minor\s+(?P<minor>\d+)",
    re.IGNORECASE,
)
KERNEL_ASIC_RE = re.compile(
    r"(?P<bdf>[0-9A-Fa-f]{4}:[0-9A-Fa-f]{2}:"
    r"[0-9A-Fa-f]{2}\.[0-7]).*?"
    r"detected\s+ip\s+block.*?<gfx_v(?P<major>\d+)_"
    r"(?P<minor>\d+)_(?P<revision>\d+)>",
    re.IGNORECASE,
)


SECTION_NAMES = {
    "global registers": "global_registers",
    "pipe registers": "pipe_registers",
    "pipe utcl1 error registers": "pipe_utcl1_error_registers",
    "pipe headers": "pipe_headers",
    "pipe dc registers": "pipe_dc_registers",
    "queue memory registers": "queue_memory_registers",
    "queue other registers": "queue_other_registers",
    "queue wave/event registers": "queue_wave_event_registers",
    "mqd": "mqd",
    "scratch memory": "scratch_memory",
}


GC_9_4_3_PROFILE = {
    "name": "gc_9_4_3",
    "asic": "gfx_v9_4_3",
    "sources": [
        "UMR database/ip/gc_9_4_3.reg",
        "Linux gc_9_4_3_sh_mask.h",
        "Linux KFD kfd_mqd_manager_v9.c and amdgpu_amdkfd_gc_9_4_3.c",
        "Linux v9_structs.h",
        "UMR print_cpc.c and print_cpc.h",
    ],
}


def _hex(value, width=None):
    """Return a stable hexadecimal string, preserving ``None``."""
    if value is None:
        return None
    if width is None:
        return "0x%x" % value
    return "0x%0*x" % (width, value)


def _parse_hex(raw):
    try:
        return int(raw, 16)
    except (TypeError, ValueError):
        return None


def _slug(text):
    value = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return value or "unknown"


def _warning(code, message, line=None, queue=None):
    item = {"code": code, "message": message, "line": line}
    if queue is not None:
        item["queue"] = queue
    return item


def _finding(severity, code, message):
    return {"severity": severity, "code": code, "message": message}


def _register_record(name, raw, line, element_index=0):
    return {
        "name": name,
        "raw": raw,
        "value": _parse_hex(raw),
        "line": line,
        "element_index": element_index,
    }


def _register_value(queue, name):
    """Return the latest named register across all queue register sections."""
    result = None
    for section_name in (
        "queue_memory_registers",
        "queue_other_registers",
        "queue_wave_event_registers",
    ):
        for record in queue["sections"].get(section_name, []):
            if record["name"].upper() == name.upper() and record["value"] is not None:
                result = record
    return result


def _mqd_value(queue, index):
    for record in queue["mqd"]["dwords"]:
        if record["index_dword"] == index:
            return record
    return None


def _field(value, mask, shift):
    return (value & mask) >> shift


def _bool_field(value, mask):
    return bool(value & mask)


def _parse_register_row(line, line_number, previous_name=None, previous_count=0):
    """Parse named and continuation values from a CPC table row."""
    records = []
    active_name = previous_name
    element_count = previous_count

    for segment in line.split("|"):
        segment = segment.strip()
        if not segment:
            continue
        matches = list(REGISTER_RE.finditer(segment))
        if matches:
            for match in matches:
                active_name = match.group("name")
                element_count = 0
                records.append(
                    _register_record(
                        active_name,
                        match.group("raw"),
                        line_number,
                        element_count,
                    )
                )
                element_count += 1
            continue
        if active_name is not None and BARE_HEX_RE.match(segment):
            raw = segment[2:] if segment.lower().startswith("0x") else segment
            records.append(
                _register_record(active_name, raw, line_number, element_count)
            )
            element_count += 1

    return records, active_name, element_count


def _new_pipe(me, pipe, line):
    return {
        "identity": {
            "me": me,
            "pipe": pipe,
            "label": "me%d.pipe%d" % (me, pipe),
            "line": line,
        },
        "sections": {},
    }


def _new_queue(me, pipe, queue, line):
    return {
        "identity": {
            "me": me,
            "pipe": pipe,
            "queue": queue,
            "label": "me%d.pipe%d.queue%d" % (me, pipe, queue),
            "line": line,
        },
        "sections": {},
        "mqd": {"dwords": [], "row_count": 0},
        "derived": {},
        "findings": [],
        "warnings": [],
    }


def _parse_filename_context(path):
    match = FILENAME_CONTEXT_RE.search(os.path.basename(path))
    if not match:
        return {"filename_gpu_index": None, "filename_xcc_index": None}
    return {
        "filename_gpu_index": int(match.group("gpu")),
        "filename_xcc_index": (
            int(match.group("xcc")) if match.group("xcc") is not None else None
        ),
    }


def _metadata_file_candidates(root):
    commands = []
    kernels = []
    if not os.path.isdir(root):
        return commands, kernels
    for directory, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for filename in sorted(filenames):
            lowered = filename.lower()
            path = os.path.join(directory, filename)
            if lowered.endswith(".txt") and "command" in lowered:
                commands.append(path)
            if lowered.endswith(".txt") and "kernel" in lowered:
                kernels.append(path)
    return commands, kernels


def scan_bundle_metadata(root):
    """Read command and kernel metadata without requiring a bundle manifest."""
    command_files, kernel_files = _metadata_file_candidates(root)
    command_map = {}
    minor_to_bdf = {}
    bdf_to_asic = {}
    warnings = []

    for path in command_files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as stream:
                for line_number, raw_line in enumerate(stream, 1):
                    line = raw_line.strip()
                    if not line.startswith("output=") or " command=" not in line:
                        continue
                    output_text, command_text = line.split(" command=", 1)
                    output_text = output_text[len("output=") :]
                    try:
                        output_parts = shlex.split(output_text)
                    except ValueError:
                        output_parts = [output_text]
                    if not output_parts:
                        continue
                    output_basename = os.path.basename(output_parts[0])
                    instance_match = COMMAND_INSTANCE_RE.search(command_text)
                    vmp_match = COMMAND_VMP_RE.search(command_text)
                    if instance_match is None and vmp_match is None:
                        continue
                    record = {
                        "umr_instance": (
                            int(instance_match.group("instance"))
                            if instance_match is not None
                            else None
                        ),
                        "umr_vmp": (
                            int(vmp_match.group("vmp"))
                            if vmp_match is not None
                            else None
                        ),
                        "command": command_text,
                        "source": path,
                        "line": line_number,
                    }
                    old = command_map.get(output_basename)
                    if old is not None and (
                        old["umr_instance"] != record["umr_instance"]
                        or old["umr_vmp"] != record["umr_vmp"]
                    ):
                        warnings.append(
                            _warning(
                                "AMBIGUOUS_COMMAND_METADATA",
                                "conflicting command metadata for %s"
                                % output_basename,
                            )
                        )
                        continue
                    command_map[output_basename] = record
        except (IOError, OSError) as error:
            warnings.append(
                _warning(
                    "METADATA_READ_FAILED",
                    "could not read %s: %s" % (path, error),
                )
            )

    for path in kernel_files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as stream:
                for raw_line in stream:
                    minor_match = KERNEL_MINOR_RE.search(raw_line)
                    if minor_match is not None:
                        minor_to_bdf[int(minor_match.group("minor"))] = (
                            minor_match.group("bdf").lower()
                        )
                    asic_match = KERNEL_ASIC_RE.search(raw_line)
                    if asic_match is not None:
                        bdf = asic_match.group("bdf").lower()
                        bdf_to_asic[bdf] = "gfx_v%s_%s_%s" % (
                            asic_match.group("major"),
                            asic_match.group("minor"),
                            asic_match.group("revision"),
                        )
        except (IOError, OSError) as error:
            warnings.append(
                _warning(
                    "METADATA_READ_FAILED",
                    "could not read %s: %s" % (path, error),
                )
            )

    unique_asics = sorted(set(bdf_to_asic.values()))
    return {
        "command_map": command_map,
        "minor_to_bdf": minor_to_bdf,
        "bdf_to_asic": bdf_to_asic,
        "unique_asic": unique_asics[0] if len(unique_asics) == 1 else None,
        "command_files": command_files,
        "kernel_files": kernel_files,
        "warnings": warnings,
    }


def _context_for_path(path, metadata):
    context = _parse_filename_context(path)
    command = metadata.get("command_map", {}).get(os.path.basename(path))
    context.update(
        {
            "umr_instance": None,
            "umr_vmp": None,
            "pci_bdf": None,
            "asic": None,
            "metadata_sources": [],
        }
    )
    if command is not None:
        context["umr_instance"] = command["umr_instance"]
        context["umr_vmp"] = command["umr_vmp"]
        context["metadata_sources"].append(
            {"path": command["source"], "line": command["line"]}
        )
    if context["umr_instance"] is not None:
        context["pci_bdf"] = metadata.get("minor_to_bdf", {}).get(
            context["umr_instance"]
        )
    if context["pci_bdf"] is not None:
        context["asic"] = metadata.get("bdf_to_asic", {}).get(
            context["pci_bdf"]
        )
    if context["asic"] is None:
        context["asic"] = metadata.get("unique_asic")

    context["xcc"] = (
        context["umr_vmp"]
        if context["umr_vmp"] is not None
        else context["filename_xcc_index"]
    )
    context["xcc_source"] = (
        "UMR command -vmp"
        if context["umr_vmp"] is not None
        else (
            "filename"
            if context["filename_xcc_index"] is not None
            else None
        )
    )
    return context


def _resolve_profile(requested, context):
    if requested == "raw":
        return {
            "name": "raw",
            "asic": context.get("asic"),
            "selection": "explicit raw mode",
            "verified_sources": [],
            "assumptions": [
                "No ASIC-specific bit fields or MQD layout are decoded."
            ],
        }
    if requested == "gc_9_4_3":
        return {
            "name": GC_9_4_3_PROFILE["name"],
            "asic": GC_9_4_3_PROFILE["asic"],
            "selection": "explicit --profile gc_9_4_3",
            "verified_sources": list(GC_9_4_3_PROFILE["sources"]),
            "assumptions": [
                "Queue registers and MQD use the local GC 9.4.3/GFX9 layout."
            ],
        }
    if context.get("asic") == "gfx_v9_4_3":
        return {
            "name": GC_9_4_3_PROFILE["name"],
            "asic": GC_9_4_3_PROFILE["asic"],
            "selection": "sibling kernel metadata identified gfx_v9_4_3",
            "verified_sources": list(GC_9_4_3_PROFILE["sources"]),
            "assumptions": [
                "The sibling kernel capture describes the parsed GPU bundle."
            ],
        }
    return {
        "name": "raw",
        "asic": context.get("asic"),
        "selection": "auto profile could not independently identify GC 9.4.3",
        "verified_sources": [],
        "assumptions": [
            "Unknown ASIC fields remain raw; use --profile only with "
            "independent ASIC evidence."
        ],
    }


def _append_record(container, section, record):
    container["sections"].setdefault(section, []).append(record)


def _decode_pointer(raw_pointer, ring_base, ring_dwords):
    if raw_pointer is None or ring_base is None or not ring_dwords:
        return {
            "valid": False,
            "reason": "pointer, ring base, or ring size is unavailable",
            "raw_dword_offset": raw_pointer,
        }
    dword_offset = raw_pointer % ring_dwords
    slot = dword_offset // 16
    intra_slot = dword_offset % 16
    return {
        "valid": True,
        "raw_dword_offset": raw_pointer,
        "ring_dword_offset": dword_offset,
        "byte_offset": dword_offset * 4,
        "physical_address": ring_base + dword_offset * 4,
        "slot_index": slot,
        "slot_address": ring_base + slot * 64,
        "intra_slot_dword": intra_slot,
        "packet_boundary_aligned": intra_slot == 0,
    }


def _decode_gc_9_4_3(queue, derived):
    findings = queue["findings"]
    warnings = queue["warnings"]
    pq_record = _register_value(queue, "PQ_CONTROL")
    doorbell_record = _register_value(queue, "PQ_DOORBELL_CONTROL")
    vmid_record = _register_value(queue, "VMID")
    status_record = _register_value(queue, "HQ_STATUS0")
    error_record = _register_value(queue, "ERROR")
    persistent_record = _register_value(queue, "PERSISTENT_STATE")

    decoded = {
        "pq_control": None,
        "doorbell_control": None,
        "vmid": None,
        "status": None,
        "error": None,
        "persistent_state": None,
    }
    derived["decoded"] = decoded

    if pq_record is None:
        warnings.append(
            _warning(
                "MISSING_PQ_CONTROL",
                "PQ_CONTROL is unavailable; ring size and AQL mode are unknown",
                queue["identity"]["line"],
                queue["identity"]["label"],
            )
        )
        return

    pq = pq_record["value"]
    queue_size_field = _field(pq, 0x0000003F, 0)
    ring_dwords = 1 << (queue_size_field + 1)
    ring_bytes = ring_dwords * 4
    ring_slots = ring_bytes // 64
    pq_decoded = {
        "raw": pq_record["raw"],
        "value": pq,
        "line": pq_record["line"],
        "queue_size_field": queue_size_field,
        "wptr_carry": _bool_field(pq, 0x00000040),
        "rptr_carry": _bool_field(pq, 0x00000080),
        "rptr_block_size": _field(pq, 0x00003F00, 8),
        "queue_full_enable": _bool_field(pq, 0x00004000),
        "pq_empty": _bool_field(pq, 0x00008000),
        "wpp_clamp_enable": _bool_field(pq, 0x00010000),
        "endian_swap": _field(pq, 0x00060000, 17),
        "min_available_size": _field(pq, 0x00300000, 20),
        "tmz": _bool_field(pq, 0x00400000),
        "execute_disable": _bool_field(pq, 0x00800000),
        "cache_policy": _field(pq, 0x01000000, 24),
        "slot_based_wptr": _field(pq, 0x06000000, 25),
        "no_update_rptr": _bool_field(pq, 0x08000000),
        "unordered_dispatch": _bool_field(pq, 0x10000000),
        "roq_pq_ib_flip": _bool_field(pq, 0x20000000),
        "privileged_state": _bool_field(pq, 0x40000000),
        "kmd_queue": _bool_field(pq, 0x80000000),
    }
    decoded["pq_control"] = pq_decoded
    derived["queue_size"] = {
        "field": queue_size_field,
        "dwords": ring_dwords,
        "bytes": ring_bytes,
        "slots_64b": ring_slots,
        "formula": "dwords = 1 << (QUEUE_SIZE + 1); bytes = dwords * 4",
    }

    is_aql = pq_decoded["slot_based_wptr"] == 2
    derived["aql"] = {
        "slot_mapping_applicable": is_aql,
        "basis": (
            "GC9 KFD programs SLOT_BASED_WPTR=2 for AQL queues"
            if is_aql
            else "SLOT_BASED_WPTR is not the KFD AQL value 2"
        ),
        "packet_bytes": 64 if is_aql else None,
        "ring_base": derived["pq_ring_base"] if is_aql else None,
        "slot_count": ring_slots if is_aql else None,
        "rptr_mapping": None,
        "wptr_mapping": None,
        "pending": None,
    }

    if doorbell_record is not None:
        doorbell = doorbell_record["value"]
        decoded["doorbell_control"] = {
            "raw": doorbell_record["raw"],
            "value": doorbell,
            "line": doorbell_record["line"],
            "doorbell_mode": _bool_field(doorbell, 0x00000001),
            "bif_drop_policy_enabled": _bool_field(doorbell, 0x00000002),
            "doorbell_offset": _field(doorbell, 0x0FFFFFFC, 2),
            "doorbell_source": _bool_field(doorbell, 0x10000000),
            "doorbell_scheduled_hit": _bool_field(doorbell, 0x20000000),
            "doorbell_enable": _bool_field(doorbell, 0x40000000),
            "doorbell_hit": _bool_field(doorbell, 0x80000000),
            "observed_loss": None,
            "bif_drop_note": (
                "DOORBELL_BIF_DROP is a configured policy bit; its value is "
                "not evidence that a doorbell was lost."
            ),
        }

    if vmid_record is not None:
        vmid = vmid_record["value"]
        decoded["vmid"] = {
            "raw": vmid_record["raw"],
            "value": vmid,
            "line": vmid_record["line"],
            "vmid": _field(vmid, 0x0000000F, 0),
            "ib_vmid": _field(vmid, 0x00000F00, 8),
            "vqid": _field(vmid, 0x03FF0000, 16),
        }
        derived["vmid"] = decoded["vmid"]["vmid"]
        derived["ib_vmid"] = decoded["vmid"]["ib_vmid"]

    if status_record is not None:
        status = status_record["value"]
        decoded["status"] = {
            "raw": status_record["raw"],
            "value": status,
            "line": status_record["line"],
            "dequeue_status": _field(status, 0x00000003, 0),
            "dequeue_retry_count": _field(status, 0x0000000C, 2),
            "scratch_ram_initialized": _bool_field(status, 0x00000080),
            "tcl2_dirty": _bool_field(status, 0x00000100),
            "power_gating_activated": _bool_field(status, 0x00000200),
            "queue_idle": _bool_field(status, 0x40000000),
            "doorbell_updated_message_enable": _bool_field(
                status, 0x80000000
            ),
        }

    if error_record is not None:
        error = error_record["value"]
        error_bits = {
            "sua_error": _bool_field(error, 0x00000010),
            "aql_error": _bool_field(error, 0x00000020),
            "pq_utcl1_error": _bool_field(error, 0x00000100),
            "ib_utcl1_error": _bool_field(error, 0x00000200),
            "eop_utcl1_error": _bool_field(error, 0x00000400),
            "iq_utcl1_error": _bool_field(error, 0x00000800),
            "rptr_report_utcl1_error": _bool_field(error, 0x00001000),
            "wptr_poll_utcl1_error": _bool_field(error, 0x00002000),
            "semaphore_utcl1_error": _bool_field(error, 0x00004000),
            "dma_source_utcl1_error": _bool_field(error, 0x00008000),
            "dma_destination_utcl1_error": _bool_field(error, 0x00010000),
            "save_restore_utcl1_error": _bool_field(error, 0x00020000),
            "queue_utcl1_error": _bool_field(error, 0x00040000),
            "thread_control_utcl1_error": _bool_field(error, 0x00080000),
        }
        decoded["error"] = {
            "raw": error_record["raw"],
            "value": error,
            "line": error_record["line"],
            "edc_error_id": _field(error, 0x0000000F, 0),
            "bits": error_bits,
            "any_selected_error": any(error_bits.values()),
        }

    if persistent_record is not None:
        persistent = persistent_record["value"]
        decoded["persistent_state"] = {
            "raw": persistent_record["raw"],
            "value": persistent,
            "line": persistent_record["line"],
            "preload_request": _bool_field(persistent, 0x00000001),
            "preload_size": _field(persistent, 0x0003FF00, 8),
            "restore_active": _bool_field(persistent, 0x10000000),
            "relaunch_waves": _bool_field(persistent, 0x20000000),
            "queue_switch_mode": _bool_field(persistent, 0x40000000),
            "dispatch_active": _bool_field(persistent, 0x80000000),
        }

    if pq_decoded["pq_empty"]:
        findings.append(
            _finding(
                "INFO",
                "EMPTY_ACTIVE_QUEUE",
                "The HQD was active when UMR emitted it and PQ_EMPTY is set.",
            )
        )
    else:
        findings.append(
            _finding(
                "INFO",
                "ACTIVE_QUEUE_NOT_EMPTY",
                "The HQD was active when UMR emitted it and PQ_EMPTY is clear; "
                "one snapshot does not establish a hang.",
            )
        )

    if is_aql:
        aql = derived["aql"]
        aql["rptr_mapping"] = _decode_pointer(
            derived["raw_pointers"]["rptr"],
            derived["pq_ring_base"],
            ring_dwords,
        )
        aql["wptr_mapping"] = _decode_pointer(
            derived["raw_pointers"]["wptr"],
            derived["pq_ring_base"],
            ring_dwords,
        )
        rmap = aql["rptr_mapping"]
        wmap = aql["wptr_mapping"]
        pending = {
            "valid": False,
            "wraps_ring": None,
            "pending_slots_modulo_ring": None,
            "ambiguous_full_or_empty": None,
            "reason": None,
        }
        if not rmap["valid"] or not wmap["valid"]:
            pending["reason"] = "one or both pointer mappings are unavailable"
        elif not rmap["packet_boundary_aligned"] or not wmap[
            "packet_boundary_aligned"
        ]:
            pending["reason"] = (
                "raw dword pointer is inside a 64-byte packet; the CPC "
                "capture is non-atomic, so a packet interval is not asserted"
            )
        else:
            delta_dwords = (
                wmap["ring_dword_offset"] - rmap["ring_dword_offset"]
            ) % ring_dwords
            pending["valid"] = True
            pending["wraps_ring"] = (
                wmap["ring_dword_offset"] < rmap["ring_dword_offset"]
            )
            pending["pending_slots_modulo_ring"] = delta_dwords // 16
            pending["ambiguous_full_or_empty"] = (
                delta_dwords == 0 and not pq_decoded["pq_empty"]
            )
            if pending["ambiguous_full_or_empty"]:
                pending["reason"] = (
                    "equal modulo pointers with PQ_EMPTY clear are ambiguous "
                    "between a full ring and a non-atomic transition"
                )
        aql["pending"] = pending


def _derive_queue(queue, profile):
    label = queue["identity"]["label"]
    mqd_base_record = _register_value(queue, "MQD_BASE")
    pq_base_record = _register_value(queue, "PQ_BASE")
    rptr_record = _register_value(queue, "PQ_RPTR")
    wptr_lo_record = _register_value(queue, "PQ_WPTR_LO")
    wptr_hi_record = _register_value(queue, "PQ_WPTR_HI")

    mqd_base = mqd_base_record["value"] if mqd_base_record else None
    pq_base = pq_base_record["value"] if pq_base_record else None
    rptr = rptr_record["value"] if rptr_record else None
    wptr = None
    if wptr_lo_record is not None and wptr_hi_record is not None:
        wptr = (wptr_hi_record["value"] << 32) | wptr_lo_record["value"]

    derived = {
        "active": {
            "is_active": True,
            "evidence": (
                "UMR print_cpc emits an HQD section only after the live "
                "CP_HQD_ACTIVE bit 0 test"
            ),
            "source_line": queue["identity"]["line"],
        },
        "identity": {
            "me": queue["identity"]["me"],
            "pipe": queue["identity"]["pipe"],
            "queue": queue["identity"]["queue"],
        },
        "mqd_base": mqd_base,
        "mqd_base_hex": _hex(mqd_base),
        "mqd_base_raw": (
            mqd_base_record["raw"] if mqd_base_record is not None else None
        ),
        "mqd_base_formula": (
            "UMR-displayed MQD_BASE is used directly; no address shift"
        ),
        "pq_ring_base": pq_base,
        "pq_ring_base_hex": _hex(pq_base),
        "pq_ring_base_raw": (
            pq_base_record["raw"] if pq_base_record is not None else None
        ),
        "raw_pointers": {
            "units": "hardware ring dword offsets for the GC9 profile",
            "rptr": rptr,
            "rptr_raw": rptr_record["raw"] if rptr_record else None,
            "wptr_lo": (
                wptr_lo_record["value"] if wptr_lo_record else None
            ),
            "wptr_lo_raw": (
                wptr_lo_record["raw"] if wptr_lo_record else None
            ),
            "wptr_hi": (
                wptr_hi_record["value"] if wptr_hi_record else None
            ),
            "wptr_hi_raw": (
                wptr_hi_record["raw"] if wptr_hi_record else None
            ),
            "wptr": wptr,
        },
        "queue_size": None,
        "aql": None,
        "vmid": None,
        "ib_vmid": None,
        "pasid": None,
        "pasid_source": None,
        "decoded": None,
        "snapshot_caveat": (
            "CPC registers and MQD memory are read sequentially; this is not "
            "an atomic queue snapshot and does not by itself prove a hang."
        ),
    }
    queue["derived"] = derived

    if mqd_base_record is None:
        queue["warnings"].append(
            _warning(
                "MISSING_MQD_BASE",
                "MQD_BASE is unavailable",
                queue["identity"]["line"],
                label,
            )
        )
    if pq_base_record is None:
        queue["warnings"].append(
            _warning(
                "MISSING_PQ_BASE",
                "PQ_BASE is unavailable",
                queue["identity"]["line"],
                label,
            )
        )
    if wptr_lo_record is not None and wptr_hi_record is None:
        queue["warnings"].append(
            _warning(
                "TRUNCATED_WPTR",
                "PQ_WPTR_LO is present but PQ_WPTR_HI is unavailable",
                wptr_lo_record["line"],
                label,
            )
        )
    if not queue["mqd"]["dwords"]:
        queue["warnings"].append(
            _warning(
                "MQD_DUMP_UNAVAILABLE",
                "MQD dwords are absent, possibly because the VRAM read "
                "failed or the log was truncated",
                queue["identity"]["line"],
                label,
            )
        )

    if profile["name"] != "gc_9_4_3":
        queue["warnings"].append(
            _warning(
                "PROFILE_UNKNOWN",
                "ASIC profile is unknown; addresses and register values are "
                "retained but ASIC-specific fields are not decoded",
                queue["identity"]["line"],
                label,
            )
        )
        return

    saved_active = _mqd_value(queue, 130)
    if saved_active is not None and not (saved_active["value"] & 1):
        queue["warnings"].append(
            _warning(
                "STALE_MQD_ACTIVE_VALUE",
                "saved MQD dword 130 has ACTIVE=0 although UMR observed the "
                "live HQD active; saved MQD state may be stale",
                saved_active["line"],
                label,
            )
        )

    mqd_lo = _mqd_value(queue, 128)
    mqd_hi = _mqd_value(queue, 129)
    if mqd_base is not None and mqd_lo is not None and mqd_hi is not None:
        saved_mqd_base = (mqd_hi["value"] << 32) | mqd_lo["value"]
        derived["mqd_saved_base"] = saved_mqd_base
        derived["mqd_saved_base_hex"] = _hex(saved_mqd_base)
        derived["mqd_base_matches_saved"] = saved_mqd_base == mqd_base
        if saved_mqd_base != mqd_base:
            queue["warnings"].append(
                _warning(
                    "MQD_BASE_MISMATCH",
                    "live MQD_BASE %s differs from saved MQD base %s"
                    % (_hex(mqd_base), _hex(saved_mqd_base)),
                    mqd_lo["line"],
                    label,
                )
            )

    _decode_gc_9_4_3(queue, derived)


def parse_log(path, requested_profile="auto", metadata=None):
    """Parse one CPC text log into the stable per-log schema."""
    if metadata is None:
        metadata = scan_bundle_metadata(os.path.dirname(os.path.abspath(path)))
    context = _context_for_path(path, metadata)
    profile = _resolve_profile(requested_profile, context)
    result = {
        "source": {
            "path": os.path.abspath(path),
            "filename": os.path.basename(path),
            "line_count": 0,
        },
        "context": context,
        "decoder_profile": profile,
        "global_sections": {},
        "pipes": [],
        "queues": [],
        "warnings": [],
        "findings": [
            _finding(
                "INFO",
                "NON_ATOMIC_CAPTURE",
                "UMR reads CPC registers and MQD memory sequentially; a "
                "single capture must not be treated as proof of a hang.",
            )
        ],
    }

    current_pipe = None
    current_queue = None
    current_section = None
    row_name = None
    row_count = 0
    pending_register = None
    mqd_next_index = None
    scratch_next_index = None

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            lines = stream.readlines()
    except (IOError, OSError) as error:
        raise RuntimeError("could not read %s: %s" % (path, error))

    result["source"]["line_count"] = len(lines)
    for line_number, raw_line in enumerate(lines, 1):
        line = ANSI_RE.sub("", raw_line.rstrip("\r\n"))
        stripped = line.strip()
        if not stripped or set(stripped) == set("-"):
            continue

        queue_match = QUEUE_RE.match(stripped)
        if queue_match is not None:
            current_queue = _new_queue(
                int(queue_match.group("me")),
                int(queue_match.group("pipe")),
                int(queue_match.group("queue")),
                line_number,
            )
            result["queues"].append(current_queue)
            current_section = None
            row_name = None
            row_count = 0
            pending_register = None
            mqd_next_index = None
            scratch_next_index = None
            continue

        pipe_match = PIPE_RE.match(stripped)
        if pipe_match is not None:
            current_pipe = _new_pipe(
                int(pipe_match.group("me")),
                int(pipe_match.group("pipe")),
                line_number,
            )
            result["pipes"].append(current_pipe)
            current_queue = None
            current_section = None
            row_name = None
            row_count = 0
            pending_register = None
            mqd_next_index = None
            scratch_next_index = None
            continue

        section_match = SECTION_RE.match(stripped)
        if section_match is not None:
            section_text = section_match.group(1)
            known_section = SECTION_NAMES.get(section_text.lower())
            if (
                known_section is None
                and current_section is not None
                and re.match(r"^[A-Z][A-Z0-9_]*$", section_text)
            ):
                pending_register = {
                    "name": section_text,
                    "line": line_number,
                }
                row_name = section_text
                row_count = 0
                continue
            current_section = known_section or _slug(section_text)
            row_name = None
            row_count = 0
            pending_register = None
            mqd_next_index = None
            scratch_next_index = None
            if current_section in ("global_registers", "scratch_memory"):
                current_pipe = None
                current_queue = None
            if current_section != "mqd":
                if current_queue is not None:
                    current_queue["sections"].setdefault(current_section, [])
                elif current_pipe is not None:
                    current_pipe["sections"].setdefault(current_section, [])
                else:
                    result["global_sections"].setdefault(current_section, [])
            continue

        if current_queue is not None and current_section == "mqd":
            mqd_match = MQD_ROW_RE.match(line)
            if mqd_match is not None:
                base_index = int(mqd_match.group("index"), 16)
                body = mqd_match.group("body")
            else:
                body = stripped
                continuation_segments = [
                    segment.strip()
                    for segment in body.split("|")
                    if segment.strip()
                ]
                if (
                    mqd_next_index is None
                    or not continuation_segments
                    or not all(
                        BARE_HEX_RE.match(segment)
                        for segment in continuation_segments
                    )
                ):
                    current_queue["warnings"].append(
                        _warning(
                            "MALFORMED_MQD_ROW",
                            "could not parse MQD row",
                            line_number,
                            current_queue["identity"]["label"],
                        )
                    )
                    continue
                base_index = mqd_next_index
            segments = [
                segment.strip()
                for segment in body.split("|")
                if segment.strip()
            ]
            valid_values = 0
            for offset, segment in enumerate(segments):
                token = segment[2:] if segment.lower().startswith("0x") else segment
                if not BARE_HEX_RE.match(segment):
                    current_queue["warnings"].append(
                        _warning(
                            "MALFORMED_MQD_VALUE",
                            "ignored malformed MQD token %r" % segment,
                            line_number,
                            current_queue["identity"]["label"],
                        )
                    )
                    continue
                current_queue["mqd"]["dwords"].append(
                    {
                        "index_dword": base_index + offset,
                        "index_hex": _hex(base_index + offset),
                        "raw": token,
                        "value": _parse_hex(token),
                        "line": line_number,
                    }
                )
                valid_values += 1
            mqd_next_index = base_index + len(segments)
            current_queue["mqd"]["row_count"] += 1
            if valid_values == 0:
                current_queue["warnings"].append(
                    _warning(
                        "EMPTY_MQD_ROW",
                        "MQD row contained no valid dwords",
                        line_number,
                        current_queue["identity"]["label"],
                    )
                )
            continue

        if current_section == "scratch_memory":
            scratch_match = MQD_ROW_RE.match(line)
            if scratch_match is not None:
                base_index = int(scratch_match.group("index"), 16)
                body = scratch_match.group("body")
            else:
                body = stripped
                continuation_segments = [
                    segment.strip()
                    for segment in body.split("|")
                    if segment.strip()
                ]
                if (
                    scratch_next_index is None
                    or not continuation_segments
                    or not all(
                        BARE_HEX_RE.match(segment)
                        for segment in continuation_segments
                    )
                ):
                    result["warnings"].append(
                        _warning(
                            "MALFORMED_SCRATCH_ROW",
                            "could not parse scratch-memory row",
                            line_number,
                        )
                    )
                    continue
                base_index = scratch_next_index
            segments = [
                segment.strip()
                for segment in body.split("|")
                if segment.strip()
            ]
            valid_values = 0
            for offset, segment in enumerate(segments):
                token = (
                    segment[2:]
                    if segment.lower().startswith("0x")
                    else segment
                )
                if not BARE_HEX_RE.match(segment):
                    result["warnings"].append(
                        _warning(
                            "MALFORMED_SCRATCH_VALUE",
                            "ignored malformed scratch token %r" % segment,
                            line_number,
                        )
                    )
                    continue
                result["global_sections"]["scratch_memory"].append(
                    _register_record(
                        "SCRATCH",
                        token,
                        line_number,
                        base_index + offset,
                    )
                )
                valid_values += 1
            scratch_next_index = base_index + len(segments)
            if valid_values == 0:
                result["warnings"].append(
                    _warning(
                        "EMPTY_SCRATCH_ROW",
                        "scratch-memory row contained no valid dwords",
                        line_number,
                    )
                )
            continue

        if current_section is None:
            if ":" in stripped:
                result["warnings"].append(
                    _warning(
                        "ROW_OUTSIDE_SECTION",
                        "ignored a row outside a recognized section",
                        line_number,
                    )
                )
            continue

        if pending_register is not None:
            first_segment = stripped.split("|", 1)[0].strip()
            if BARE_HEX_RE.match(first_segment):
                raw = (
                    first_segment[2:]
                    if first_segment.lower().startswith("0x")
                    else first_segment
                )
                record = _register_record(
                    pending_register["name"],
                    raw,
                    pending_register["line"],
                    0,
                )
                records = [record]
                remainder = (
                    stripped.split("|", 1)[1] if "|" in stripped else ""
                )
                more, row_name, row_count = _parse_register_row(
                    remainder,
                    line_number,
                    pending_register["name"],
                    1,
                )
                records.extend(more)
                pending_register = None
            else:
                result["warnings"].append(
                    _warning(
                        "MALFORMED_REGISTER_ROW",
                        "register %s had no value"
                        % pending_register["name"],
                        pending_register["line"],
                    )
                )
                pending_register = None
                records, row_name, row_count = _parse_register_row(
                    line, line_number, row_name, row_count
                )
        else:
            records, row_name, row_count = _parse_register_row(
                line, line_number, row_name, row_count
            )

        if not records:
            pending_match = PENDING_REGISTER_RE.search(stripped)
            if pending_match is not None:
                pending_register = {
                    "name": pending_match.group("name"),
                    "line": line_number,
                }
                continue
            if ":" in stripped:
                target_warnings = (
                    current_queue["warnings"]
                    if current_queue is not None
                    else result["warnings"]
                )
                target_warnings.append(
                    _warning(
                        "MALFORMED_REGISTER_ROW",
                        "could not parse register row",
                        line_number,
                        (
                            current_queue["identity"]["label"]
                            if current_queue is not None
                            else None
                        ),
                    )
                )
            continue

        for record in records:
            if current_queue is not None:
                _append_record(current_queue, current_section, record)
            elif current_pipe is not None:
                _append_record(current_pipe, current_section, record)
            else:
                result["global_sections"].setdefault(
                    current_section, []
                ).append(record)

    if pending_register is not None:
        result["warnings"].append(
            _warning(
                "TRUNCATED_REGISTER_ROW",
                "register %s had no value before end of file"
                % pending_register["name"],
                pending_register["line"],
            )
        )

    if not result["global_sections"] and not result["pipes"] and not result["queues"]:
        result["warnings"].append(
            _warning(
                "NO_CPC_STRUCTURE",
                "no CPC sections, pipe identities, or queue identities were found",
            )
        )
    if not result["queues"]:
        result["findings"].append(
            _finding(
                "INFO",
                "NO_ACTIVE_QUEUES_EMITTED",
                "No active HQD sections were present in this capture.",
            )
        )

    for queue in result["queues"]:
        _derive_queue(queue, profile)

    result["summary"] = _summarize_log(result)
    return result


def _summarize_log(log):
    active = 0
    empty = 0
    nonempty = 0
    unknown_state = 0
    queue_warnings = 0
    for queue in log["queues"]:
        if queue["derived"].get("active", {}).get("is_active"):
            active += 1
        pq = (queue["derived"].get("decoded") or {}).get("pq_control")
        if pq is None:
            unknown_state += 1
        elif pq["pq_empty"]:
            empty += 1
        else:
            nonempty += 1
        queue_warnings += len(queue["warnings"])
    return {
        "pipe_count": len(log["pipes"]),
        "queue_count": len(log["queues"]),
        "active_queue_count": active,
        "empty_active_queue_count": empty,
        "nonempty_active_queue_count": nonempty,
        "unknown_state_queue_count": unknown_state,
        "warning_count": len(log["warnings"]) + queue_warnings,
    }


def discover_logs(input_path):
    """Return sorted CPC log paths for a file, CPC directory, or bundle."""
    absolute = os.path.abspath(input_path)
    if os.path.isfile(absolute):
        return [absolute]
    if not os.path.isdir(absolute):
        raise RuntimeError("input does not exist: %s" % input_path)

    candidates = []
    for directory, dirnames, filenames in os.walk(absolute):
        dirnames.sort()
        for filename in sorted(filenames):
            lowered = filename.lower()
            if not lowered.endswith(".txt"):
                continue
            if "umr_cpc" not in lowered:
                continue
            candidates.append(os.path.join(directory, filename))
    if not candidates:
        raise RuntimeError(
            "no matching *umr_cpc*.txt logs found under %s" % input_path
        )
    return sorted(candidates)


def _make_mqd_query(logs, address):
    matches = []
    inactive_candidates = []
    for log in logs:
        for queue in log["queues"]:
            if queue["derived"].get("mqd_base") != address:
                continue
            item = {
                "source": log["source"]["path"],
                "context": dict(log["context"]),
                "queue": dict(queue["identity"]),
                "mqd_base": address,
                "mqd_base_hex": _hex(address),
                "active": queue["derived"]["active"]["is_active"],
                "scope": "active HQD in this file's GPU/XCC context",
            }
            if item["active"]:
                matches.append(item)
            else:
                inactive_candidates.append(item)

    if len(matches) == 0:
        status = "zero"
    elif len(matches) == 1:
        status = "unique"
    else:
        status = "multiple"
    warnings = [
        (
            "MQD addresses are interpreted only in an active HQD and the "
            "same file GPU/XCC context; saved MQD values from inactive queues "
            "can be stale."
        )
    ]
    for match in matches:
        if match["context"].get("xcc") is None:
            warnings.append(
                "XCC context is unknown for %s; same-XCC identity cannot be "
                "independently confirmed." % match["source"]
            )
    return {
        "requested_address": address,
        "requested_address_hex": _hex(address),
        "status": status,
        "match_count": len(matches),
        "matches": matches,
        "inactive_candidates": inactive_candidates,
        "warnings": warnings,
    }


def build_report(input_path, requested_profile="auto", mqd_address=None):
    paths = discover_logs(input_path)
    metadata_root = (
        os.path.dirname(os.path.abspath(input_path))
        if os.path.isfile(os.path.abspath(input_path))
        else os.path.abspath(input_path)
    )
    metadata = scan_bundle_metadata(metadata_root)
    logs = [
        parse_log(path, requested_profile=requested_profile, metadata=metadata)
        for path in paths
    ]
    summary = {
        "file_count": len(logs),
        "pipe_count": sum(log["summary"]["pipe_count"] for log in logs),
        "queue_count": sum(log["summary"]["queue_count"] for log in logs),
        "active_queue_count": sum(
            log["summary"]["active_queue_count"] for log in logs
        ),
        "empty_active_queue_count": sum(
            log["summary"]["empty_active_queue_count"] for log in logs
        ),
        "nonempty_active_queue_count": sum(
            log["summary"]["nonempty_active_queue_count"] for log in logs
        ),
        "unknown_state_queue_count": sum(
            log["summary"]["unknown_state_queue_count"] for log in logs
        ),
        "warning_count": len(metadata["warnings"])
        + sum(log["summary"]["warning_count"] for log in logs),
    }
    return {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": "parse_cpc.py", "version": TOOL_VERSION},
        "input": {
            "path": os.path.abspath(input_path),
            "kind": (
                "file" if os.path.isfile(os.path.abspath(input_path)) else "directory"
            ),
            "requested_profile": requested_profile,
        },
        "summary": summary,
        "logs": logs,
        "mqd_query": (
            _make_mqd_query(logs, mqd_address)
            if mqd_address is not None
            else None
        ),
        "warnings": metadata["warnings"],
    }


def _context_text(context):
    return (
        "filename-GPU=%s UMR-instance=%s PCI=%s XCC=%s"
        % (
            (
                context["filename_gpu_index"]
                if context["filename_gpu_index"] is not None
                else "?"
            ),
            context["umr_instance"] if context["umr_instance"] is not None else "?",
            context["pci_bdf"] or "?",
            context["xcc"] if context["xcc"] is not None else "?",
        )
    )


def render_text(report):
    lines = [
        "UMR CPC report",
        "==============",
        "Input: %s" % report["input"]["path"],
        (
            "Files: %(file_count)d  Queues: %(queue_count)d active, "
            "%(empty_active_queue_count)d empty, "
            "%(nonempty_active_queue_count)d non-empty, "
            "%(unknown_state_queue_count)d state-unknown"
        )
        % report["summary"],
        "Warnings: %d" % report["summary"]["warning_count"],
        (
            "Caveat: CPC is a sequential, non-atomic snapshot; this report "
            "does not declare a hang."
        ),
        "",
    ]

    query = report.get("mqd_query")
    if query is not None:
        lines.extend(
            [
                "MQD lookup %s: %s (%d match%s)"
                % (
                    query["requested_address_hex"],
                    query["status"],
                    query["match_count"],
                    "" if query["match_count"] == 1 else "es",
                )
            ]
        )
        for match in query["matches"]:
            lines.append(
                "  %s -> %s [%s]"
                % (
                    query["requested_address_hex"],
                    match["queue"]["label"],
                    _context_text(match["context"]),
                )
            )
        for warning in query["warnings"]:
            lines.append("  WARNING: %s" % warning)
        lines.append("")

    for log in report["logs"]:
        context = log["context"]
        lines.append(
            "%s [%s]"
            % (log["source"]["filename"], _context_text(context))
        )
        lines.append(
            "  Profile: %s (%s)"
            % (
                log["decoder_profile"]["name"],
                log["decoder_profile"]["selection"],
            )
        )
        lines.append(
            "  Active queues: %(active_queue_count)d "
            "(empty=%(empty_active_queue_count)d, "
            "non-empty=%(nonempty_active_queue_count)d, "
            "unknown=%(unknown_state_queue_count)d)"
            % log["summary"]
        )
        shown_queues = log["queues"]
        if query is not None:
            matching_labels = {
                (match["source"], match["queue"]["label"])
                for match in query["matches"]
            }
            shown_queues = [
                queue
                for queue in log["queues"]
                if (log["source"]["path"], queue["identity"]["label"])
                in matching_labels
            ]
        for queue in shown_queues:
            derived = queue["derived"]
            size = derived.get("queue_size")
            pq = (derived.get("decoded") or {}).get("pq_control")
            lines.append(
                "  %s: MQD=%s PQ=%s"
                % (
                    queue["identity"]["label"],
                    derived["mqd_base_hex"] or "?",
                    derived["pq_ring_base_hex"] or "?",
                )
            )
            lines.append(
                "    RPTR=%s WPTR=%s%s"
                % (
                    _hex(derived["raw_pointers"]["rptr"]) or "?",
                    _hex(derived["raw_pointers"]["wptr"]) or "?",
                    (
                        " size=%d dwords/%d bytes/%d slots"
                        % (
                            size["dwords"],
                            size["bytes"],
                            size["slots_64b"],
                        )
                        if size is not None
                        else ""
                    ),
                )
            )
            if pq is not None:
                vmid = (derived.get("decoded") or {}).get("vmid")
                lines.append(
                    "    PQ_EMPTY=%d AQL-slot-mode=%s VMID=%s IB_VMID=%s"
                    % (
                        1 if pq["pq_empty"] else 0,
                        pq["slot_based_wptr"],
                        vmid["vmid"] if vmid is not None else "?",
                        vmid["ib_vmid"] if vmid is not None else "?",
                    )
                )
            for warning in queue["warnings"]:
                lines.append(
                    "    WARNING %s: %s"
                    % (warning["code"], warning["message"])
                )
        for warning in log["warnings"]:
            lines.append(
                "  WARNING %s%s: %s"
                % (
                    warning["code"],
                    (
                        " line %s" % warning["line"]
                        if warning.get("line") is not None
                        else ""
                    ),
                    warning["message"],
                )
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _parse_address(text):
    try:
        value = int(text, 0)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "ADDRESS must be an integer such as 0x01795000"
        )
    if value < 0:
        raise argparse.ArgumentTypeError("ADDRESS must not be negative")
    return value


def _argument_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Parse one UMR --tool cpc log or recursively parse all "
            "*umr_cpc*.txt logs in a directory/bundle."
        )
    )
    parser.add_argument("input", metavar="LOG_OR_DIRECTORY")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit the stable JSON schema instead of human-readable text",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        help="write output to PATH instead of stdout",
    )
    parser.add_argument(
        "--mqd",
        metavar="ADDRESS",
        type=_parse_address,
        help=(
            "find exact MQD_BASE matches in active queues, scoped to each "
            "file's GPU/XCC context"
        ),
    )
    parser.add_argument(
        "--profile",
        choices=("auto", "gc_9_4_3", "raw"),
        default="auto",
        help=(
            "decoder profile (default: auto from sibling kernel metadata; "
            "raw disables ASIC-specific decoding)"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s " + TOOL_VERSION,
    )
    return parser


def main(argv=None):
    parser = _argument_parser()
    args = parser.parse_args(argv)
    try:
        report = build_report(
            args.input,
            requested_profile=args.profile,
            mqd_address=args.mqd,
        )
    except RuntimeError as error:
        print("parse_cpc.py: error: %s" % error, file=sys.stderr)
        return EXIT_INPUT_ERROR

    if args.json_output:
        output = json.dumps(report, indent=2, sort_keys=True) + "\n"
    else:
        output = render_text(report)

    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as stream:
                stream.write(output)
        except (IOError, OSError) as error:
            print(
                "parse_cpc.py: error: could not write %s: %s"
                % (args.output, error),
                file=sys.stderr,
            )
            return EXIT_INPUT_ERROR
    else:
        sys.stdout.write(output)

    if report["mqd_query"] is not None:
        status = report["mqd_query"]["status"]
        if status == "zero":
            return EXIT_NO_MQD_MATCH
        if status == "multiple":
            return EXIT_MULTIPLE_MQD_MATCHES
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
