#!/usr/bin/env python3
"""Focused tests for Queue Doctor's KFD/UMR queue namespace bridge."""

import importlib.util
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("02_umr_queue_doctor.py")
COLLECTOR_PATH = MODULE_PATH.with_name("06_collect_all_gpu_debug.sh")
SPEC = importlib.util.spec_from_file_location("umr_queue_doctor", MODULE_PATH)
doctor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(doctor)


def umr_queue(queue_id, queue_type, mqd_address):
    return doctor.QueueInfo(
        queue_id=queue_id,
        queue_type=queue_type,
        queue_type_name=doctor.QUEUE_TYPE_NAMES[queue_type],
        mqd_gpu_address=mqd_address,
        rptr=0,
        wptr=0,
        selected=False,
    )


class QueueNamespaceMappingTests(unittest.TestCase):
    MULTI_GPU_MQDS = """\
Process 556522 PASID 556522:
  Compute queue on device f220
    00000000: 00000000 00000000 00000000 00000000 00000000 00000000 00000000 00000000
    00000200: 00179000 00000000 00000000 00000000 00000000 00000000 00000000 00000000
  SDMA queue on device 1234
    00000000: 00000000 00000000 00000000 00000000 00000000 00000000 00000000 00000000
Process 999999 PASID 999999:
  Compute queue on device f220
    00000200: deadbeef 00000000 00000000 00000000 00000000 00000000 00000000 00000000
"""

    def test_parses_pid_scoped_mqd_order_and_hex_gpu_id(self):
        identities = doctor.parse_kfd_mqd_queue_inventory(
            self.MULTI_GPU_MQDS, 556522
        )

        self.assertEqual([item.queue_id for item in identities], [0, 1])
        self.assertEqual([item.gpu_id for item in identities], [61984, 0x1234])
        self.assertEqual([item.queue_type for item in identities], [1, 3])
        self.assertEqual(identities[0].mqd_gpu_address, 0x179000)
        self.assertIsNone(identities[1].mqd_gpu_address)

    def test_observed_global_sysfs_ids_are_not_compared_to_umr_indices(self):
        # KFD sysfs IDs 41 and 77 are process-global. UMR reports local
        # indices 0 and 1 from the same PID MQD sequence.
        kfd_queues = [
            doctor.KfdQueueInfo(41, 61984),
            doctor.KfdQueueInfo(77, 0x1234),
        ]
        identities = doctor.parse_kfd_mqd_queue_inventory(
            self.MULTI_GPU_MQDS, 556522
        )
        findings = []

        selected = doctor.map_umr_queues_to_gpu(
            queues=[
                umr_queue(0, 1, "0x179000"),
                umr_queue(1, 3, "<unavailable>"),
            ],
            target_gpu_id=61984,
            process_gpu_ids=[queue.gpu_id for queue in kfd_queues],
            before=identities,
            after=identities,
            findings=findings,
            snapshot_index=0,
        )

        self.assertEqual([queue.queue_id for queue in selected], [0])
        self.assertEqual(selected[0].kfd_gpu_id, 61984)
        self.assertNotIn(0, {queue.queue_id for queue in kfd_queues})
        self.assertEqual(findings[-1].code, "UMR_QUEUE_NAMESPACE_MAPPED")

    def test_single_gpu_preserves_existing_all_queue_behavior(self):
        queues = [
            umr_queue(0, 1, "0x1000"),
            umr_queue(1, 3, "<unavailable>"),
        ]

        selected = doctor.map_umr_queues_to_gpu(
            queues=queues,
            target_gpu_id=61984,
            process_gpu_ids=[61984, 61984],
            before=[],
            after=[],
            findings=[],
            snapshot_index=0,
        )

        self.assertEqual(selected, queues)
        self.assertTrue(
            all(queue.mapping_source == "single-process-gpu" for queue in queues)
        )

    def test_missing_multi_gpu_identity_skips_gpu_instead_of_dumping(self):
        findings = []

        selected = doctor.map_umr_queues_to_gpu(
            queues=[umr_queue(0, 1, "0x179000")],
            target_gpu_id=61984,
            process_gpu_ids=[61984, 0x1234],
            before=[],
            after=[],
            findings=findings,
            snapshot_index=0,
        )

        self.assertEqual(selected, [])
        self.assertEqual(findings[0].code, "UMR_QUEUE_NAMESPACE_UNAVAILABLE")

    def test_address_mismatch_skips_only_ambiguous_target_queue(self):
        before = [
            doctor.MqdQueueIdentity(0, 61984, 1, 0x1000),
            doctor.MqdQueueIdentity(1, 61984, 1, 0x2000),
            doctor.MqdQueueIdentity(2, 0x1234, 3),
        ]
        findings = []

        selected = doctor.map_umr_queues_to_gpu(
            queues=[
                umr_queue(0, 1, "0x1000"),
                umr_queue(1, 1, "0x9999"),
                umr_queue(2, 3, "<unavailable>"),
            ],
            target_gpu_id=61984,
            process_gpu_ids=[61984, 0x1234],
            before=before,
            after=before,
            findings=findings,
            snapshot_index=0,
        )

        self.assertEqual([queue.queue_id for queue in selected], [0])
        self.assertIn(
            "UMR_QUEUE_MQD_ADDRESS_MISMATCH",
            [finding.code for finding in findings],
        )

    def test_queue_order_change_rejects_entire_snapshot(self):
        before = [doctor.MqdQueueIdentity(0, 61984, 1, 0x1000)]
        after = [doctor.MqdQueueIdentity(0, 0x1234, 1, 0x1000)]
        findings = []

        selected = doctor.map_umr_queues_to_gpu(
            queues=[umr_queue(0, 1, "0x1000")],
            target_gpu_id=61984,
            process_gpu_ids=[61984, 0x1234],
            before=before,
            after=after,
            findings=findings,
            snapshot_index=0,
        )

        self.assertEqual(selected, [])
        self.assertEqual(findings[0].code, "KFD_QUEUE_SET_CHANGED")

    def test_inventory_lists_target_queue_ids_without_unrelated_gpu_noise(self):
        target = doctor.NodeInfo(
            6,
            61984,
            "MI308X",
            "0001:0b:00.0",
            1,
            0xB00,
            160,
            120100,
            4,
        )
        unrelated = doctor.NodeInfo(
            7,
            12345,
            "MI308X",
            "0001:2b:00.0",
            1,
            0x2B00,
            168,
            120100,
            5,
        )
        inventory = doctor.GpuInventory(
            queues=[
                doctor.KfdQueueInfo(41, 61984),
                doctor.KfdQueueInfo(77, 61984),
            ],
            target_nodes=[target],
            selected_nodes=[target],
            visible_nodes=[target, unrelated],
            amd_smi_gpu_bdfs={
                4: "0001:0b:00.0",
                5: "0001:2b:00.0",
            },
        )

        summary = doctor.render_target_gpu_inventory(556522, inventory)

        self.assertIn("Target GPUs", summary)
        self.assertIn("AMD-SMI=4 / KFD node=6 / gpu_id=61984", summary)
        self.assertIn("KFD queues=2 / KFD queue IDs=[41, 77]", summary)
        self.assertIn("Queue Doctor selected GPUs:", summary)
        self.assertIn("DUMP AMD-SMI=4", summary)
        self.assertNotIn("visible GPUs", summary)
        self.assertNotIn("owns no KFD queue", summary)
        self.assertNotIn("AMD-SMI=5", summary)
        self.assertIn("never joined by numeric equality", summary)

        visible = doctor.without_target_gpu_inventory(
            summary + "\nUMR Queue Doctor\nCapture summary\n",
            summary,
        )
        self.assertEqual(visible, "UMR Queue Doctor\nCapture summary\n")
        self.assertNotIn("Source of truth:", visible)
        self.assertNotIn("AMD-SMI=4 / KFD node=6", visible)
        self.assertNotIn("Queue ID namespaces:", visible)

        inventory_data = doctor.inventory_to_dict(556522, inventory)
        self.assertEqual(
            [gpu["gpu_id"] for gpu in inventory_data["target_gpus"]],
            [61984],
        )
        self.assertEqual(inventory_data["amd_smi_gpu_bdfs"], {4: "0001:0b:00.0"})
        self.assertNotIn("unrelated_" + "visible_gpus", inventory_data)


class QueueDumpLayoutTests(unittest.TestCase):
    def setUp(self):
        self.queue = umr_queue(7, 1, "0x1000")

    def test_pending_and_full_ring_have_distinct_paths_and_commands(self):
        base = ["/usr/bin/umr", "--by-pci", "0000:01:00.0"]
        pending_command = doctor.queue_dump_command(base, 1234, self.queue, "pending")
        full_command = doctor.queue_dump_command(base, 1234, self.queue, "full-ring")

        self.assertNotIn("use_full_user_queue", pending_command)
        self.assertIn("use_full_user_queue", full_command)
        self.assertNotEqual(
            doctor.queue_dump_path(Path("/tmp/q"), 2, self.queue, "pending"),
            doctor.queue_dump_path(Path("/tmp/q"), 2, self.queue, "full-ring"),
        )
        self.assertEqual(
            doctor.queue_dump_path(Path("/tmp/q"), 2, self.queue, "pending").parts[-2],
            "pending",
        )
        self.assertEqual(
            doctor.queue_dump_path(Path("/tmp/q"), 2, self.queue, "full-ring").parts[-2],
            "full-ring",
        )
        self.assertEqual(
            doctor.queue_dump_path(Path("/tmp/q"), 2, self.queue, "pending").suffix,
            ".txt",
        )
        self.assertEqual(
            doctor.queue_dump_path(Path("/tmp/q"), 2, self.queue, "full-ring").suffix,
            ".txt",
        )

    def test_timeout_is_recorded_without_affecting_other_dump(self):
        pending = doctor.CommandResult(["umr", "pending"], 0, "Done decoding IB\n")
        full = doctor.CommandResult(
            ["umr", "-O", "use_full_user_queue"],
            124,
            "[tool] command timed out\n",
            timed_out=True,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pending_path = doctor.queue_dump_path(root, 0, self.queue, "pending")
            full_path = doctor.queue_dump_path(root, 0, self.queue, "full-ring")
            pending_path.parent.mkdir(parents=True)
            full_path.parent.mkdir(parents=True)
            doctor.write_command_log(pending_path, pending)
            doctor.write_command_log(full_path, full)

            self.assertIn("[exit=0]", pending_path.read_text())
            self.assertIn("[exit=124]", full_path.read_text())
            self.assertIn("timed out", full_path.read_text())

    def test_full_ring_count_is_separate_from_pending_count(self):
        pending = doctor.PacketDump(
            7, "pending", "/tmp/pending", 0, False, 4, 1, 4, {}, True, 60, False, []
        )
        full = doctor.PacketDump(
            7, "full-ring", "/tmp/full", 124, False, None, 0, 0, {}, False,
            60, True, ["umr", "-O", "use_full_user_queue"]
        )
        snapshot = doctor.Snapshot(0, "now", None, None, packet_dumps=[pending, full])
        self.assertEqual(
            doctor.packet_dump_counts([snapshot]),
            {
                "pending_total": 1,
                "pending_success": 1,
                "full_ring_total": 1,
                "full_ring_success": 0,
            },
        )


class FullRingCliTests(unittest.TestCase):
    @staticmethod
    def _parse(*arguments):
        with mock.patch("sys.argv", [str(MODULE_PATH), *arguments]):
            return doctor.parse_args()

    def test_raw_capture_is_retained_by_default(self):
        self.assertTrue(self._parse().keep_full_ring_raw)

    def test_legacy_keep_flag_remains_default_true(self):
        self.assertTrue(
            self._parse("--keep-full-ring-raw").keep_full_ring_raw
        )

    def test_discard_flag_opts_out_of_successful_raw_retention(self):
        self.assertFalse(
            self._parse("--discard-full-ring-raw").keep_full_ring_raw
        )


class CollectorOutputRoutingTests(unittest.TestCase):
    def test_inventory_block_is_persisted_without_stdout_tee(self):
        collector = COLLECTOR_PATH.read_text(encoding="utf-8")
        routing = collector[
            collector.index("run_queue_target_inventory()") :
            collector.index("run_aql_queue_dump()")
        ]

        self.assertIn('"$@" >"$stdout_file" 2>"$stderr_file"', routing)
        self.assertIn('} >>"$RUN_LOG"', routing)
        self.assertIn('} >>"$SUMMARY"', routing)
        self.assertIn('tee -a "$RUN_LOG" <"$stderr_file" >&2', routing)
        self.assertNotIn('log "BEGIN ${step}"', routing)
        self.assertNotIn('log "END ${step}', routing)
        self.assertNotIn('"$stdout_file" | tee', routing)


class FullRingDecodeFixtureTests(unittest.TestCase):
    @staticmethod
    def _compute_state(base_address="0x7f0744e00000", ring_size=128):
        state = doctor.QueueState(4)
        state.queue_type = 1
        state.queue_type_name = "compute"
        state.hqd_base_address = base_address
        state.ring_size_bytes = ring_size
        return state

    @staticmethod
    def _vm_output(data):
        return "\n".join(
            f"0x{offset:x}: "
            + " ".join(f"{byte:02x}" for byte in data[offset:offset + 16])
            for offset in range(0, len(data), 16)
        )

    def test_valid_packets_and_exact_physical_slot_count(self):
        kernel = bytearray(64)
        struct.pack_into("<H", kernel, 0, 2 | (1 << 8))
        struct.pack_into("<H", kernel, 4, 64)
        struct.pack_into("<Q", kernel, 32, 0x12340000)
        struct.pack_into("<Q", kernel, 56, 0x4000)
        barrier = bytearray(64)
        struct.pack_into("<H", barrier, 0, 3)
        struct.pack_into("<Q", barrier, 8, 0x4080)
        struct.pack_into("<Q", barrier, 56, 0x4100)
        slots = doctor.decode_full_ring(bytes(kernel + barrier), 0x100000, 128,
                                        7, 9, {0x4080: {"status": "PENDING"}})
        self.assertEqual(len(slots), 2)
        self.assertEqual(slots[0]["packet_type"], "KERNEL_DISPATCH")
        self.assertEqual(slots[0]["barrier_bit"], 1)
        self.assertEqual(slots[1]["packet_type"], "BARRIER_AND")
        self.assertEqual(slots[1]["signals"]["0x4080"]["status"], "PENDING")

    def test_non_wrapped_pending_interval_is_present_in_full_ring_output(self):
        data = bytearray(8 * 64)
        for slot in range(8):
            struct.pack_into("<H", data, slot * 64, 2)

        slots = doctor.decode_full_ring(bytes(data), 0x100000, len(data), 2, 5)
        pending = [slot for slot in slots if slot["pending"]]
        rendered = "\n".join(
            line
            for slot in slots
            for line in doctor.format_full_ring_slot(slot)
        )

        self.assertEqual([slot["slot"] for slot in pending], [2, 3, 4])
        self.assertEqual(
            [slot["logical_index"] for slot in pending],
            [2, 3, 4],
        )
        self.assertEqual(rendered.count("Opcode "), 8)
        for logical_index, offset in ((2, 0x80), (3, 0xC0), (4, 0x100)):
            self.assertIn(
                f"+ 0x{offset:04x}]",
                rendered,
            )
            self.assertIn(
                f"[PENDING logical={logical_index}]",
                rendered,
            )

    def test_wrapped_pending_interval_is_present_in_full_ring_output(self):
        data = bytearray(8 * 64)
        for slot in range(8):
            struct.pack_into("<H", data, slot * 64, 2)

        slots = doctor.decode_full_ring(bytes(data), 0x200000, len(data), 6, 2)
        pending_by_slot = {
            slot["slot"]: slot["logical_index"]
            for slot in slots
            if slot["pending"]
        }
        rendered = "\n".join(
            line
            for slot in slots
            for line in doctor.format_full_ring_slot(slot)
        )

        self.assertEqual(pending_by_slot, {0: 8, 1: 9, 6: 6, 7: 7})
        self.assertEqual(rendered.count("Opcode "), 8)
        for logical_index in (6, 7, 8, 9):
            self.assertIn(
                f"[PENDING logical={logical_index}]",
                rendered,
            )

    def test_native_pending_headers_correlate_to_full_ring_slots(self):
        data = bytearray(8 * 64)
        struct.pack_into("<I", data, 2 * 64, 0x00000001)
        struct.pack_into("<I", data, 3 * 64, 0x00000B02)
        slots = doctor.decode_full_ring(
            bytes(data), 0x100000, len(data), 10, 12
        )
        native = """\
[0x0@0x100080 + 0x0000] [0x00000001] Opcode 0x1 [HSA_INVALID] (32 words)
[0x0@0x100080 + 0x0040] [0x00000b02] Opcode 0x2 [HSA_KERNEL_DISPATCH] (32 words)
"""

        correlation = doctor.correlate_pending_dump(
            native,
            ring_base=0x100000,
            ring_size=len(data),
            pending=doctor.pending_interval_layout(10, 12, 8),
            slots=slots,
        )

        self.assertEqual(correlation["status"], "exact")
        self.assertFalse(correlation["atomic"])
        self.assertEqual(correlation["native_packet_count"], 2)
        self.assertEqual(correlation["executable_native_packets"], 1)
        self.assertEqual(correlation["executable_packets_matched"], 1)
        self.assertTrue(correlation["all_executable_packets_matched"])

    def test_pending_correlation_compares_16_bit_aql_header(self):
        data = bytearray(2 * 64)
        # The upper half of the first dword is packet body, not AQL header:
        # setup_dimensions=1 for dispatch and amd_format=2 for vendor packets.
        struct.pack_into("<I", data, 0, 0x00010B02)
        struct.pack_into("<I", data, 64, 0x00021500)
        slots = doctor.decode_full_ring(
            bytes(data), 0x180000, len(data), 0, 2
        )
        native = """\
[0x0@0x180000 + 0x0000] [0x00000b02] Opcode 0x2 [HSA_KERNEL_DISPATCH] (32 words)
[0x0@0x180000 + 0x0002] [0x0001] |---> setup_dimensions=1
[0x0@0x180000 + 0x0040] [0x00001500] Opcode 0x0 [HSA_VENDOR_SPECIFIC] (32 words)
"""

        correlation = doctor.correlate_pending_dump(
            native,
            ring_base=0x180000,
            ring_size=len(data),
            pending=doctor.pending_interval_layout(0, 2, 2),
            slots=slots,
        )

        self.assertEqual(correlation["status"], "exact")
        self.assertEqual(correlation["executable_packets_matched"], 2)
        self.assertIn("16-bit AQL header", correlation["comparison"])

    def test_wrapped_native_address_is_canonicalized_to_physical_slot(self):
        data = bytearray(4 * 64)
        struct.pack_into("<I", data, 3 * 64, 0x00000B02)
        struct.pack_into("<I", data, 0, 0x00001500)
        slots = doctor.decode_full_ring(
            bytes(data), 0x200000, len(data), 3, 1
        )
        native = """\
[0x0@0x2000c0 + 0x0000] [0x00000b02] Opcode 0x2 [HSA_KERNEL_DISPATCH] (32 words)
[0x0@0x2000c0 + 0x0040] [0x00001500] Opcode 0x0 [HSA_VENDOR_SPECIFIC] (32 words)
"""

        correlation = doctor.correlate_pending_dump(
            native,
            ring_base=0x200000,
            ring_size=len(data),
            pending=doctor.pending_interval_layout(3, 1, 4),
            slots=slots,
        )

        self.assertEqual(correlation["status"], "exact")
        self.assertEqual(correlation["executable_packets_matched"], 2)

    def test_pending_correlation_detects_header_mismatch(self):
        data = bytearray(64)
        struct.pack_into("<I", data, 0, 0x00000B02)
        slots = doctor.decode_full_ring(bytes(data), 0x300000, 64, 0, 1)
        native = (
            "[0x0@0x300000 + 0x0000] [0x00001502] "
            "Opcode 0x2 [HSA_KERNEL_DISPATCH] (32 words)\n"
        )

        correlation = doctor.correlate_pending_dump(
            native,
            ring_base=0x300000,
            ring_size=64,
            pending=doctor.pending_interval_layout(0, 1, 1),
            slots=slots,
        )

        self.assertEqual(correlation["status"], "mismatch")
        self.assertEqual(len(correlation["mismatches"]), 1)
        self.assertFalse(correlation["all_executable_packets_matched"])

    def test_queue_vm_partition_prefers_selected_queue_xcc(self):
        queue = umr_queue(4, 1, "0x1000")
        state = self._compute_state()
        state.compute_current_logic_xcc_id = 0

        command, partition, source = doctor.queue_vm_command(
            ["umr", "--vm-partition", "1"], queue, state
        )

        self.assertEqual(command[-1], "0")
        self.assertEqual(partition, 0)
        self.assertEqual(
            source, "--print-uq compute_current_logic_xcc_id"
        )

    def test_invalid_slot_remains_authoritative_in_human_output(self):
        data = bytearray(64)
        struct.pack_into("<H", data, 0, 1)
        struct.pack_into("<H", data, 4, 32)
        struct.pack_into("<Q", data, 32, 0x2000)
        slot = doctor.decode_full_ring(bytes(data), 0x200000, 64, 1, 1, {})
        self.assertEqual(slot[0]["packet_type"], "INVALID")
        self.assertEqual(slot[0]["residual_body_type"], "KERNEL_DISPATCH")
        self.assertTrue(slot[0]["heuristic_residual_body"])
        self.assertEqual(len(bytes.fromhex(slot[0]["raw"])), 64)
        rendered = "\n".join(doctor.format_full_ring_slot(slot[0]))
        self.assertEqual(
            rendered.splitlines()[0],
            "[0x0@0x200000 + 0x0000]\t[        0x00000001]\t"
            "Opcode 0x1 [HSA_INVALID] (32 words, type: 0, hdr: 0x1)",
        )
        self.assertNotIn("residual_body_type=", rendered)
        self.assertNotIn("(heuristic; ownership/state unknown)", rendered)
        self.assertIn(
            "[0x0@0x200000 + 0x0004]\t[            0x0020]\t"
            "|---> workgroup_size_x=32",
            rendered,
        )
        self.assertIn("|---> kernel_object=0x2000", rendered)
        self.assertIn("|---> completion_signal=0x0", rendered)
        self.assertNotIn("status=UNAVAILABLE", rendered)
        self.assertNotIn("reason=null signal handle", rendered)
        self.assertNotIn("raw_64_bytes", rendered)
        self.assertNotIn("inferred/stale", rendered)
        self.assertNotIn("consumed", rendered.lower())

    def test_unreadable_completion_signal_emits_only_handle(self):
        data = bytearray(64)
        struct.pack_into("<H", data, 0, 2)
        struct.pack_into("<Q", data, 56, 0x4140)
        slot = doctor.decode_aql_slot(
            bytes(data),
            0x510000,
            0,
            None,
            {
                0x4140: {
                    "status": "UNAVAILABLE",
                    "reason": "fixture unavailable",
                    "read_exit": 9,
                }
            },
        )

        lines = doctor.format_full_ring_slot(slot)

        self.assertEqual(
            [line for line in lines if "+ 0x0038]" in line],
            [
                "[0x0@0x510000 + 0x0038]\t[0x0000000000004140]\t"
                "|---> completion_signal=0x4140"
            ],
        )

    def test_barrier_dependency_signal_details_are_role_specific(self):
        data = bytearray(64)
        struct.pack_into("<H", data, 0, 3)
        struct.pack_into("<Q", data, 16, 0x4080)
        struct.pack_into("<Q", data, 24, 0x40C0)
        struct.pack_into("<Q", data, 32, 0x4100)
        slot = doctor.decode_aql_slot(
            bytes(data),
            0x500000,
            0,
            None,
            {
                0x4080: {
                    "value": 1,
                    "status": "PENDING",
                    "reason": "fixture reason",
                    "read_exit": 7,
                },
                0x40C0: {
                    "status": "UNAVAILABLE",
                    "reason": "fixture unavailable",
                    "read_exit": 9,
                },
                0x4100: {
                    "value": 0,
                    "status": "COMPLETED",
                    "reason": "must not be rendered",
                    "read_exit": 0,
                },
            },
        )

        lines = doctor.format_full_ring_slot(slot)

        self.assertEqual(
            [line for line in lines if "+ 0x0008]" in line],
            [
                "[0x0@0x500000 + 0x0008]\t[0x0000000000000000]\t"
                "|---> dep_signal[0]=0x0"
            ],
        )
        self.assertEqual(
            [line for line in lines if "+ 0x0010]" in line],
            [
                "[0x0@0x500000 + 0x0010]\t[0x0000000000004080]\t"
                "|---> dep_signal[1]=0x4080",
                "[0x0@0x500000 + 0x0010]\t[                  ]\t"
                "|     |---> value=1 (0x0000000000000001)",
                "[0x0@0x500000 + 0x0010]\t[                  ]\t"
                "|     |---> status=PENDING",
            ],
        )
        self.assertEqual(
            [line for line in lines if "+ 0x0018]" in line],
            [
                "[0x0@0x500000 + 0x0018]\t[0x00000000000040c0]\t"
                "|---> dep_signal[2]=0x40c0",
            ],
        )
        self.assertEqual(
            [line for line in lines if "+ 0x0020]" in line],
            [
                "[0x0@0x500000 + 0x0020]\t[0x0000000000004100]\t"
                "|---> dep_signal[3]=0x4100",
                "[0x0@0x500000 + 0x0020]\t[                  ]\t"
                "|     |---> value=0 (0x0000000000000000)",
                "[0x0@0x500000 + 0x0020]\t[                  ]\t"
                "|     |---> status=COMPLETED",
            ],
        )
        self.assertEqual(
            [line for line in lines if "+ 0x0038]" in line],
            [
                "[0x0@0x500000 + 0x0038]\t[0x0000000000000000]\t"
                "|---> completion_signal=0x0",
            ],
        )

    def test_vendor_body_and_signal_use_umr_style_offsets(self):
        data = bytearray(64)
        struct.pack_into("<H", data, 0, 0)
        struct.pack_into("<H", data, 2, 2)
        struct.pack_into("<Q", data, 8, 0x4080)
        struct.pack_into("<q", data, 16, 7)
        struct.pack_into("<Q", data, 24, 0xFF)
        struct.pack_into("<I", data, 32, 3)
        slot = doctor.decode_aql_slot(
            bytes(data),
            0x3000C0,
            3,
            None,
            {0x4080: {"value": 1, "status": "PENDING", "read_exit": 0}},
        )

        rendered = "\n".join(doctor.format_full_ring_slot(slot))

        self.assertIn(
            "[0x0@0x300000 + 0x00c0]\t[        0x00000000]\t"
            "Opcode 0x0 [HSA_VENDOR_SPECIFIC] "
            "(32 words, type: 0, hdr: 0x0)",
            rendered,
        )
        self.assertIn(
            "+ 0x00c2]\t[            0x0002]\t|---> amd_format=2",
            rendered,
        )
        self.assertIn(
            "+ 0x00c8]\t[0x0000000000004080]\t|---> watch_signal=0x4080",
            rendered,
        )
        self.assertIn(
            "+ 0x00d0]\t[0x0000000000000007]\t|---> compare_value=7",
            rendered,
        )
        self.assertIn(
            "+ 0x00d8]\t[0x00000000000000ff]\t|---> mask=0xff",
            rendered,
        )
        self.assertIn(
            "+ 0x00e0]\t[        0x00000003]\t|---> condition=3",
            rendered,
        )
        self.assertIn("|     |---> value=1 (0x0000000000000001)", rendered)
        self.assertIn("|     |---> status=PENDING", rendered)

    def test_signal_fixture_handles_null_stale_and_valid_states(self):
        self.assertEqual(doctor._signal_status(1), "PENDING")
        self.assertEqual(doctor._signal_status(0), "COMPLETED")
        self.assertEqual(doctor._signal_status(-1), "COMPLETED")
        cache = {}
        self.assertEqual(doctor._signal_read(lambda: None, 0, 1, cache)["status"],
                         "UNAVAILABLE")
        self.assertEqual(doctor._signal_read(lambda: None, 3, 1, cache)["status"],
                         "UNAVAILABLE")

    def test_queue_state_metadata_includes_doorbell_and_dispatch_fields(self):
        output = """\
Queue #4:
  queue_type: 1 (compute)
  hqd_base_addr: 0x100000
  rb_buf_size: 0x200
  queue.4.MQD[010] == 0x22 (doorbell_offset)
  queue.4.MQD[011] == 0x33 (read_dispatch_id)
  queue.4.MQD[012] == 0x44 (write_dispatch_id)
  queue.4.MQD[039] == 0x3 (compute_current_logic_xcc_id)
  queue.4.MQD[225] == 0x2 (pm4_target_xcc_in_xcp)
"""
        state = doctor.parse_queue_states(output)[0]
        self.assertEqual(state.hqd_base_address, 0x100000)
        self.assertEqual(state.doorbell_id, 0x22)
        self.assertEqual(state.memory_read_dispatch_id, 0x33)
        self.assertEqual(state.memory_write_dispatch_id, 0x44)
        self.assertEqual(state.compute_current_logic_xcc_id, 3)
        self.assertEqual(state.pm4_target_xcc_in_xcp, 2)
        self.assertEqual(state.rb_buf_size_words, 0x200)
        self.assertEqual(state.ring_size_bytes, 0x800)

    def test_vm_read_parser_preserves_requested_byte_count(self):
        output = "0x1000: 00 01 02 03 04 05 06 07\n"
        self.assertEqual(
            doctor.parse_vm_read_bytes(output, 8),
            bytes(range(8)),
        )
        command = doctor.vm_read_command(["umr"], 0x1000, 1024)
        self.assertEqual(command[-2:], ["0@0x1000", "0x400"])

    def test_hex_compute_base_captures_exact_raw_size_and_slot_count(self):
        kernel = bytearray(64)
        struct.pack_into("<H", kernel, 0, 2 | (1 << 8))
        struct.pack_into("<H", kernel, 4, 64)
        struct.pack_into("<Q", kernel, 32, 0x12340000)
        struct.pack_into("<Q", kernel, 56, 0x4000)
        barrier = bytearray(64)
        struct.pack_into("<H", barrier, 0, 3)
        struct.pack_into("<Q", barrier, 16, 0x4080)
        data = bytes(kernel + barrier)
        ring_result = doctor.BinaryCommandResult(
            ["umr", "--vm-read"], 1, data
        )
        signal = bytearray(64)
        struct.pack_into("<q", signal, 8, 1)
        signal_result = doctor.BinaryCommandResult(
            ["umr", "--vm-read"], 1, bytes(signal)
        )
        queue = umr_queue(4, 1, "0x1000")
        state = self._compute_state()
        findings = []

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "full-ring").mkdir()
            with mock.patch.object(
                doctor, "run_binary_command",
                side_effect=[ring_result, signal_result, signal_result]
            ) as run_command:
                artifact = doctor.capture_full_ring(
                    base_command=["umr"],
                    pid=1234,
                    queue=queue,
                    state=state,
                    output_dir=output_dir,
                    snapshot_index=0,
                    timeout=1,
                    findings=findings,
                )

            self.assertEqual(run_command.call_count, 3)
            decoded_path = Path(artifact["decoded_path"])
            expected_raw_path = output_dir / "full-ring" / (
                "snapshot-00-queue-4-compute.bin"
            )
            self.assertEqual(artifact["ring_base"], 0x7F0744E00000)
            self.assertEqual(artifact["byte_count"], 128)
            self.assertEqual(artifact["decoded_slots"], 2)
            self.assertTrue(artifact["decode_complete"])
            self.assertEqual(Path(artifact["raw_path"]), expected_raw_path)
            self.assertTrue(artifact["raw_retained"])
            self.assertTrue(expected_raw_path.exists())
            self.assertEqual(expected_raw_path.read_bytes(), data)
            self.assertEqual(
                artifact["raw_retention_reason"],
                "successful captures are retained by default",
            )
            decoded = decoded_path.read_text()
            self.assertIn(
                "Queue AMDGPU Queue <unavailable>:4 "
                "(device <unavailable>, QID 4) [HSA/compute]",
                decoded,
            )
            self.assertIn("  Ring base:   0x7f0744e00000", decoded)
            self.assertIn("  Ring size:   128 bytes (2 packet slots)", decoded)
            self.assertIn("  Read index:  0 (slot 0)", decoded)
            self.assertIn("  Write index: 0 (slot 0)", decoded)
            self.assertIn("  Pending:     0 packet(s)", decoded)
            self.assertIn("  Doorbell ID:", decoded)
            self.assertIn("  Decoded slots: 2 / 2", decoded)
            self.assertIn("retain successful .bin captures (default)", decoded)
            self.assertIn(f"Raw VM-read artifact: {expected_raw_path}", decoded)
            self.assertIn(f"Decoded artifact: {decoded_path}", decoded)
            self.assertIn("Raw VM-read bytes: retained at", decoded)
            self.assertIn(
                "Dumping 0x20 words from user queue-4 "
                "(complete physical ring, from word 0x0 to 0x20):",
                decoded,
            )
            self.assertIn(
                "Decoding IB at 0x0@0x7f0744e00000 "
                "from 0x0@0x7f0744e00000 of 0x40 words (type 0)",
                decoded,
            )
            kernel_golden = (
                "[0x0@0x7f0744e00000 + 0x0000]\t"
                "[        0x00000102]\t"
                "Opcode 0x2 [HSA_KERNEL_DISPATCH] "
                "(32 words, type: 0, hdr: 0x102)\n"
                "[0x0@0x7f0744e00000 + 0x0002]\t"
                "[            0x0000]\t|---> setup_dimensions=0\n"
                "[0x0@0x7f0744e00000 + 0x0004]\t"
                "[            0x0040]\t|---> workgroup_size_x=64"
            )
            self.assertIn(kernel_golden, decoded)
            self.assertIn(
                "[0x0@0x7f0744e00000 + 0x0040]\t"
                "[        0x00000003]\t"
                "Opcode 0x3 [HSA_BARRIER_AND] "
                "(32 words, type: 0, hdr: 0x3)",
                decoded,
            )
            self.assertIn("|---> kernel_object=0x12340000", decoded)
            self.assertIn("|---> completion_signal=0x4000", decoded)
            barrier_dependency_golden = (
                "[0x0@0x7f0744e00000 + 0x0048]\t"
                "[0x0000000000000000]\t|---> dep_signal[0]=0x0\n"
                "[0x0@0x7f0744e00000 + 0x0050]\t"
                "[0x0000000000004080]\t|---> dep_signal[1]=0x4080\n"
                "[0x0@0x7f0744e00000 + 0x0050]\t"
                "[                  ]\t"
                "|     |---> value=1 (0x0000000000000001)\n"
                "[0x0@0x7f0744e00000 + 0x0050]\t"
                "[                  ]\t|     |---> status=PENDING"
            )
            self.assertIn(barrier_dependency_golden, decoded)
            barrier_completion_golden = (
                "[0x0@0x7f0744e00000 + 0x0078]\t"
                "[0x0000000000000000]\t|---> completion_signal=0x0"
            )
            self.assertIn(barrier_completion_golden, decoded)
            self.assertIn("|     |---> status=PENDING", decoded)
            self.assertNotIn("status=UNAVAILABLE", decoded)
            self.assertNotIn("reason=null signal handle", decoded)
            self.assertNotIn("raw_64_bytes", decoded)
            self.assertFalse(
                any(line.startswith("{") for line in decoded.splitlines())
            )
            self.assertEqual(len(artifact["slots"]), 2)
            self.assertEqual(findings, [])

    def test_capture_uses_dword_capacity_and_records_gpu_vm_context(self):
        state = doctor.parse_queue_states("""\
Queue #4:
  queue_type: 1 (compute)
  hqd_base_addr: 0x7f0744e00000
  hqd_rptr_addr: 0x7f0752944080 (0x19bb7)
  rb_wptr_poll_addr: 0x7f0752944038 (0x19cc4)
  rb_buf_size: 0x40000
  queue.4.MQD[039] == 0x0 (compute_current_logic_xcc_id)
  queue.4.MQD[225] == 0x0 (pm4_target_xcc_in_xcp)
""")[0]
        queue = umr_queue(4, 1, "0x1000")
        queue.rptr = 0x19BB7
        queue.wptr = 0x19CC4
        queue.kfd_gpu_id = 63420
        result = doctor.BinaryCommandResult(["umr", "--vm-read"], 5, b"")
        base_command = [
            "umr",
            "--by-pci",
            "0000:0a:00.0",
            "--vm-partition",
            "1",
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "full-ring").mkdir()
            with mock.patch.object(
                doctor, "run_binary_command", return_value=result
            ) as run_command:
                artifact = doctor.capture_full_ring(
                    base_command=base_command,
                    pid=2056320,
                    queue=queue,
                    state=state,
                    output_dir=output_dir,
                    snapshot_index=0,
                    timeout=1,
                    findings=[],
                )

        command = artifact["capture_command"]
        self.assertEqual(state.rb_buf_size_words, 0x40000)
        self.assertEqual(artifact["ring_size_bytes"], 0x100000)
        self.assertEqual(artifact["ring_slot_capacity"], 0x4000)
        self.assertEqual(
            command[-2:],
            ["0@0x7f0744e00000", "0x100000"],
        )
        self.assertIn(
            "kfd,pid=2056320,queue=4",
            command,
        )
        self.assertEqual(artifact["pending_interval"]["read_slot"], 0x1BB7)
        self.assertEqual(artifact["pending_interval"]["write_slot"], 0x1CC4)
        self.assertEqual(artifact["pending_interval"]["pending_count"], 0x10D)
        self.assertEqual(
            artifact["capture_context"],
            {
                "gpu_pci_bdf": "0000:0a:00.0",
                "kfd_gpu_id": 63420,
                "umr_queue_id": 4,
                "user_queue_selector": "kfd,pid=2056320,queue=4",
                "vm_partition": 1,
                "xcc_context_source": "--vm-partition",
                "mqd_compute_current_logic_xcc_id": 0,
                "mqd_pm4_target_xcc_in_xcp": 0,
                "vmid": 0,
                "vm_hub": "gfx",
                "address_space": "process GPUVA from UMR --user-queue context",
            },
        )

    def test_successful_decode_discards_raw_only_when_requested(self):
        data = bytes(128)
        result = doctor.BinaryCommandResult(["umr", "--vm-read"], 1, data)
        queue = umr_queue(4, 1, "0x1000")
        state = self._compute_state()

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "full-ring").mkdir()
            with mock.patch.object(
                doctor, "run_binary_command", return_value=result
            ):
                artifact = doctor.capture_full_ring(
                    base_command=["umr"],
                    pid=1234,
                    queue=queue,
                    state=state,
                    output_dir=output_dir,
                    snapshot_index=0,
                    timeout=1,
                    findings=[],
                    keep_raw=False,
                )

            self.assertTrue(artifact["decode_complete"])
            self.assertFalse(artifact["raw_retained"])
            self.assertIsNone(artifact["raw_path"])
            self.assertFalse(Path(artifact["raw_capture_path"]).exists())
            decoded = Path(artifact["decoded_path"]).read_text()
            self.assertIn("--discard-full-ring-raw", decoded)
            self.assertIn("discarded after successful decode", decoded)

    def test_decode_failure_retains_raw_and_reports_reason(self):
        data = bytes(128)
        result = doctor.BinaryCommandResult(["umr", "--vm-read"], 1, data)
        queue = umr_queue(4, 1, "0x1000")
        state = self._compute_state()
        findings = []

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "full-ring").mkdir()
            with mock.patch.object(
                doctor, "run_binary_command", return_value=result
            ), \
                    mock.patch.object(
                        doctor,
                        "decode_full_ring",
                        side_effect=ValueError("fixture decode failure"),
                    ):
                artifact = doctor.capture_full_ring(
                    base_command=["umr"],
                    pid=1234,
                    queue=queue,
                    state=state,
                    output_dir=output_dir,
                    snapshot_index=2,
                    timeout=1,
                    findings=findings,
                    keep_raw=False,
                )

            self.assertFalse(artifact["decode_complete"])
            self.assertEqual(
                artifact["error"],
                "full-ring decode failed: fixture decode failure",
            )
            self.assertEqual(Path(artifact["raw_path"]).stat().st_size, 128)
            decoded = Path(artifact["decoded_path"]).read_text()
            self.assertIn("retained at", decoded)
            self.assertIn("fixture decode failure", decoded)
            self.assertEqual(findings[-1].code, "FULL_RING_CAPTURE_FAILED")

    def test_summary_lists_default_raw_and_decoded_artifacts(self):
        raw_path = "/tmp/full-ring/snapshot-00-queue-4-compute.bin"
        decoded_path = (
            "/tmp/full-ring/snapshot-00-queue-4-compute.decoded.txt"
        )
        snapshot = doctor.Snapshot(
            0,
            "now",
            None,
            None,
            full_ring_artifacts=[{
                "queue_id": 4,
                "raw_path": raw_path,
                "decoded_path": decoded_path,
                "decode_complete": True,
            }],
        )
        node = doctor.NodeInfo(
            1, 1234, "GPU", "0000:01:00.0", 0, 0, 128, 120100, 0
        )
        args = mock.Mock(
            pid=1234,
            interval=0,
            keep_full_ring_raw=True,
        )

        summary = doctor.render_summary(
            args=args,
            node=node,
            umr=Path("/usr/bin/umr"),
            process_name="test",
            output_dir=Path("/tmp"),
            snapshots=[snapshot],
            findings=[],
        )

        self.assertIn("retain successful .bin captures (default)", summary)
        self.assertIn(f"raw={raw_path}", summary)
        self.assertIn(f"decoded={decoded_path}", summary)

    def test_hex_compute_base_failure_is_isolated_to_artifact(self):
        result = doctor.BinaryCommandResult(["umr", "--vm-read"], 5, b"")
        queue = umr_queue(4, 1, "0x1000")
        state = self._compute_state()
        findings = []

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "full-ring").mkdir()
            with mock.patch.object(
                doctor, "run_binary_command", return_value=result
            ):
                artifact = doctor.capture_full_ring(
                    base_command=["umr"],
                    pid=1234,
                    queue=queue,
                    state=state,
                    output_dir=output_dir,
                    snapshot_index=1,
                    timeout=1,
                    findings=findings,
                )

            self.assertEqual(artifact["exit"], 5)
            self.assertEqual(artifact["error"], "UMR VM read produced no bytes")
            self.assertEqual(artifact["decoded_slots"], 0)
            self.assertIsNone(artifact["raw_path"])
            self.assertTrue(Path(artifact["decoded_path"]).is_file())
            self.assertEqual(findings[-1].code, "FULL_RING_CAPTURE_FAILED")


if __name__ == "__main__":
    unittest.main()
