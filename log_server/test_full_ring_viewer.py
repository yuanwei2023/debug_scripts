#!/usr/bin/env python3
"""Focused tests for Queue Doctor full-ring pointer visualization."""

import http.client
import importlib.util
import tempfile
import threading
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("server.py")
SPEC = importlib.util.spec_from_file_location("full_ring_server", str(MODULE_PATH))
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)
viewer = server.full_ring_viewer


def full_ring_text(
    *,
    base=0x100000,
    slots=8,
    rptr=2,
    wptr=5,
    include_pointers=True,
    atomicity="non-atomic (separate live commands)",
    padding_per_slot="",
):
    lines = [
        "==========================================================================",
        "Queue AMDGPU Queue KFD gpu_id 7:0 (device KFD gpu_id 7, QID 0) [HSA/compute]",
        "  Ring base:   0x{:x}".format(base),
        "  Ring size:   {} bytes ({} packet slots)".format(slots * 64, slots),
    ]
    if include_pointers:
        lines.extend(
            [
                "  Read index:  {} (slot {}) [@ 0x200080]".format(
                    rptr, rptr % slots
                ),
                "  Write index: {} (slot {}) [@ 0x200038]".format(
                    wptr, wptr % slots
                ),
                "  Pointer source: --print-uq logical HSA indices",
                "  Capture atomicity: " + atomicity,
            ]
        )
    lines.extend(
        [
            "  UMR user-queue context: kfd,pid=12,queue=0",
            "  Decoded slots: {} / {}".format(slots, slots),
            "==========================================================================",
            "  Full physical-slot dump. HSA_INVALID is authoritative.",
            "  [PENDING logical=N] marks [RPTR, WPTR).",
        ]
    )
    if wptr >= rptr:
        pending = set(range(rptr, wptr))
    else:
        count = ((wptr % slots) - (rptr % slots)) % slots or slots
        pending = set(range(rptr, rptr + count))
    logical_by_slot = {logical % slots: logical for logical in pending}
    for slot in range(slots):
        annotation = (
            " [PENDING logical={}]".format(logical_by_slot[slot])
            if slot in logical_by_slot
            else ""
        )
        lines.append(
            "[0x0@0x{:x} + 0x{:04x}]\t[0x00000001]\t"
            "Opcode 0x1 [HSA_INVALID]{}".format(base, slot * 64, annotation)
        )
        lines.append(
            "[0x0@0x{:x} + 0x{:04x}]\t[0x{:08x}]\t"
            "|---> body_slot={}".format(base, slot * 64 + 4, slot, slot)
        )
        lines.append(
            "[0x0@0x{:x} + 0x{:04x}]\t[                  ]\t"
            "|     |---> signal_slot={} status=COMPLETED".format(
                base, slot * 64 + 56, slot
            )
        )
        if padding_per_slot:
            lines.append(padding_per_slot + str(slot))
    return "\n".join(lines) + "\n"


class FullRingPointerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "bundle"
        self.full_ring = self.root / "queue_doctor" / "full-ring"
        self.full_ring.mkdir(parents=True)

    def tearDown(self):
        self.temporary.cleanup()

    def write_artifact(self, text, name="snapshot-00-queue-0-compute.decoded.txt"):
        path = self.full_ring / name
        path.write_text(text)
        return path

    def inspect(self, path):
        with path.open("rb") as stream:
            return viewer.inspect_full_ring(
                stream, str(self.root), str(path.relative_to(self.root))
            )

    def test_real_style_no_wrap_metadata_and_pointer_arithmetic(self):
        metadata = self.inspect(self.write_artifact(full_ring_text()))
        self.assertIsNotNone(metadata)
        self.assertTrue(metadata["self_contained"])
        self.assertEqual(metadata["pending_count"], 3)
        self.assertFalse(metadata["wraps"])
        self.assertEqual(metadata["rptr_view"]["slot"], 2)
        self.assertEqual(metadata["rptr_view"]["byte_offset"], 0x80)
        self.assertEqual(metadata["rptr_view"]["address"], 0x100080)
        self.assertEqual(metadata["wptr_view"]["slot"], 5)
        self.assertEqual(metadata["wptr_view"]["address"], 0x100140)

    def test_wrap_and_equal_pointer_roles_preserve_pending_markers(self):
        wrapped_path = self.write_artifact(
            full_ring_text(rptr=6, wptr=2),
            "snapshot-00-queue-0-wrapped.decoded.txt",
        )
        wrapped = self.inspect(wrapped_path)
        self.assertTrue(wrapped["wraps"])
        self.assertEqual(wrapped["pending_count"], 4)
        text = wrapped_path.read_text()
        self.assertIn("[PENDING logical=6]", text)
        self.assertIn("[PENDING logical=9]", text)

        equal_path = self.write_artifact(
            full_ring_text(rptr=3, wptr=3),
            "snapshot-01-queue-0-equal.decoded.txt",
        )
        equal = self.inspect(equal_path)
        lines = equal_path.read_text().splitlines()
        roles = viewer.pointer_roles_for_lines(
            lines, list(range(1, len(lines) + 1)), equal
        )
        self.assertEqual(
            [role_set for role_set in roles.values() if role_set],
            [{"rptr", "wptr"}],
        )
        self.assertEqual(equal["pending_count"], 0)

    def test_complete_symmetric_slot_windows_wrap_without_duplicates(self):
        path = self.write_artifact(
            full_ring_text(slots=128, rptr=0, wptr=1),
            "snapshot-00-queue-0-large.decoded.txt",
        )
        metadata = self.inspect(path)
        with path.open("rb") as stream:
            result = viewer.read_slot_window(
                stream, metadata, "rptr", center_slot=0, context=16
            )
        self.assertTrue(result["available"])
        window = result["window"]
        self.assertEqual(window["slot_sequence"], list(range(112, 128)) + list(range(17)))
        self.assertEqual(window["rendered_slot_count"], 33)
        self.assertEqual(len(set(window["slot_sequence"])), 33)
        self.assertTrue(window["wraps"])
        rendered = "\n".join(result["lines"])
        for slot in (112, 127, 0, 1, 16):
            self.assertIn("body_slot={}".format(slot), rendered)
            self.assertIn("signal_slot={}".format(slot), rendered)
        target_index = result["line_numbers"].index(result["target_line"])
        self.assertGreater(target_index, 0)
        self.assertLess(target_index, len(result["lines"]) - 1)

    def test_first_last_and_small_equal_ring_have_complete_blocks(self):
        path = self.write_artifact(
            full_ring_text(slots=8, rptr=7, wptr=7),
            "snapshot-02-queue-0-edge.decoded.txt",
        )
        metadata = self.inspect(path)
        with path.open("rb") as stream:
            last = viewer.read_slot_window(
                stream, metadata, "rptr", center_slot=7, context=16
            )
            first = viewer.read_slot_window(
                stream, metadata, "rptr", center_slot=0, context=16
            )
        for result in (last, first):
            self.assertEqual(result["window"]["rendered_slot_count"], 8)
            self.assertEqual(len(set(result["window"]["slot_sequence"])), 8)
            self.assertEqual(
                sum("Opcode " in line for line in result["lines"]), 8
            )
            self.assertEqual(
                sum("body_slot=" in line for line in result["lines"]), 8
            )

    def test_unavailable_and_ambiguous_companion_metadata_are_rejected(self):
        path = self.write_artifact(full_ring_text(include_pointers=False))
        state = self.root / "queue_doctor" / "snapshot-00-queue-state.txt"
        state.write_text(
            """Queue #0:
  hqd_base_addr: 0x100000
  hqd_rptr_addr: 0x200080 (0x2)
  rb_wptr_poll_addr: 0x200038 (0x5)
  rb_buf_size: 0x80
Queue #0:
  hqd_base_addr: 0x100000
  hqd_rptr_addr: 0x200080 (0x3)
  rb_wptr_poll_addr: 0x200038 (0x6)
  rb_buf_size: 0x80
"""
        )
        metadata = self.inspect(path)
        self.assertEqual(metadata["companion_status"], "ambiguous")
        self.assertIsNone(metadata["rptr_view"])
        self.assertIn(
            "AMBIGUOUS_COMPANION_METADATA",
            {item["code"] for item in metadata["warnings"]},
        )

        state.unlink()
        metadata = self.inspect(path)
        self.assertEqual(metadata["companion_status"], "unmatched")
        self.assertIsNone(metadata["wptr_view"])


class FullRingHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "bundle"
        self.full_ring = self.root / "queue_doctor" / "full-ring"
        self.full_ring.mkdir(parents=True)
        handler = server.make_handler(str(self.root))
        handler.log_message = lambda *args: None
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, path):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.httpd.server_address[1], timeout=10
        )
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        status = response.status
        connection.close()
        return status, body

    def test_equal_pointers_have_both_badges_anchors_themes_and_escaping(self):
        artifact = self.full_ring / "snapshot-00-queue-0-compute.decoded.txt"
        artifact.write_text(
            full_ring_text(
                rptr=1,
                wptr=1,
                atomicity='<script>alert("metadata")</script>',
            )
        )
        status, body = self.request(
            "/queue_doctor/full-ring/snapshot-00-queue-0-compute.decoded.txt"
            "?pointer=rptr"
        )
        self.assertEqual(status, 200)
        self.assertIn('id="pointer-rptr"', body)
        self.assertIn('id="pointer-wptr"', body)
        self.assertIn('class="slot-badge rptr">[RPTR]</span>', body)
        self.assertIn('class="slot-badge wptr">[WPTR]</span>', body)
        self.assertIn("?pointer=rptr#pointer-rptr", body)
        self.assertIn("?pointer=wptr#pointer-wptr", body)
        self.assertIn(":root[data-theme=\"dark\"]", body)
        self.assertIn(":root[data-theme=\"amber\"]", body)
        self.assertIn("&lt;script&gt;alert", body)
        self.assertNotIn('<script>alert("metadata")</script>', body)
        self.assertIn("next producer slot", body)
        self.assertIn("Pending is [RPTR, WPTR)", body)
        self.assertGreater(body.count("Opcode"), 1)
        pointer_position = body.index('id="pointer-rptr"')
        self.assertIn("Opcode", body[:pointer_position])
        self.assertIn("Opcode", body[pointer_position:])

    def test_truncated_default_preview_has_reachable_targeted_views(self):
        artifact = self.full_ring / "snapshot-00-queue-0-compute.decoded.txt"
        artifact.write_text(
            full_ring_text(
                slots=4096,
                rptr=3000,
                wptr=3001,
                padding_per_slot="X" * 80,
            )
        )
        url = "/queue_doctor/full-ring/snapshot-00-queue-0-compute.decoded.txt"
        status, default = self.request(url)
        self.assertEqual(status, 200)
        self.assertIn("<strong>Preview limited.</strong>", default)
        self.assertIn("?pointer=rptr#pointer-rptr", default)
        self.assertIn("?pointer=wptr#pointer-wptr", default)

        status, rptr = self.request(url + "?pointer=rptr")
        self.assertEqual(status, 200)
        self.assertIn("Targeted RPTR slot window", rptr)
        self.assertIn('id="pointer-rptr"', rptr)
        self.assertIn("0x2ee00 (192,000)", rptr)
        self.assertEqual(rptr.count("Opcode"), 49)
        self.assertIn("Previous window", rptr)
        self.assertIn("Next window", rptr)
        self.assertIn("\u00b116 slots", rptr)
        pointer_position = rptr.index('id="pointer-rptr"')
        self.assertGreater(rptr[:pointer_position].count("Opcode"), 20)
        self.assertGreater(rptr[pointer_position:].count("Opcode"), 20)
        self.assertNotIn("max-height:", rptr)

        status, wptr = self.request(url + "?pointer=wptr")
        self.assertEqual(status, 200)
        self.assertIn("Targeted WPTR slot window", wptr)
        self.assertIn('id="pointer-wptr"', wptr)
        self.assertIn("0x2ee40 (192,064)", wptr)
        self.assertEqual(wptr.count("Opcode"), 49)
        pointer_position = wptr.index('id="pointer-wptr"')
        self.assertGreater(wptr[:pointer_position].count("Opcode"), 20)
        self.assertGreater(wptr[pointer_position:].count("Opcode"), 20)

        previous_start = rptr.index("Previous window")
        previous_href_start = rptr.rfind('href="', 0, previous_start) + len('href="')
        previous_href = rptr[previous_href_start:rptr.index('"', previous_href_start)]
        status, previous = self.request(previous_href.replace("&amp;", "&"))
        self.assertEqual(status, 200)
        self.assertIn('id="window-center"', previous)
        self.assertEqual(previous.count("Opcode"), 49)

    def test_missing_pointers_disable_jump_controls(self):
        artifact = self.full_ring / "snapshot-00-queue-0-compute.decoded.txt"
        artifact.write_text(full_ring_text(include_pointers=False))
        status, body = self.request(
            "/queue_doctor/full-ring/snapshot-00-queue-0-compute.decoded.txt"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body.count('aria-disabled="true">Jump to'), 2)
        self.assertIn("Pointer metadata unavailable", body)


if __name__ == "__main__":
    unittest.main()
