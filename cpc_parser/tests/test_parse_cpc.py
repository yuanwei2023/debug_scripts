#!/usr/bin/env python3
"""Unit and CLI tests for the standalone CPC parser."""

from __future__ import print_function

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest


TEST_DIR = os.path.dirname(os.path.abspath(__file__))
PARSER_DIR = os.path.dirname(TEST_DIR)
FIXTURE_DIR = os.path.join(TEST_DIR, "fixtures")
SCRIPT = os.path.join(PARSER_DIR, "parse_cpc.py")
if PARSER_DIR not in sys.path:
    sys.path.insert(0, PARSER_DIR)

import parse_cpc  # noqa: E402


def fixture(name):
    return os.path.join(FIXTURE_DIR, name)


class ParserTests(unittest.TestCase):
    def test_normal_target_queue_and_verified_derivations(self):
        log = parse_cpc.parse_log(
            fixture("normal_target.txt"), requested_profile="gc_9_4_3"
        )
        self.assertEqual(1, log["summary"]["queue_count"])
        queue = log["queues"][0]
        derived = queue["derived"]

        self.assertEqual("me1.pipe0.queue2", queue["identity"]["label"])
        self.assertTrue(derived["active"]["is_active"])
        self.assertEqual(0x01795000, derived["mqd_base"])
        self.assertEqual("0x1795000", derived["mqd_base_hex"])
        self.assertIn("no address shift", derived["mqd_base_formula"])
        self.assertTrue(derived["mqd_base_matches_saved"])
        self.assertEqual(0x7F0744E00000, derived["pq_ring_base"])
        self.assertEqual(0x1BB9C, derived["raw_pointers"]["rptr"])
        self.assertEqual(0xD9CC40, derived["raw_pointers"]["wptr"])

        self.assertEqual(262144, derived["queue_size"]["dwords"])
        self.assertEqual(1048576, derived["queue_size"]["bytes"])
        self.assertEqual(16384, derived["queue_size"]["slots_64b"])
        self.assertEqual(3, derived["vmid"])
        self.assertEqual(3, derived["ib_vmid"])
        self.assertIsNone(derived["pasid"])

        pq = derived["decoded"]["pq_control"]
        self.assertEqual(2, pq["slot_based_wptr"])
        self.assertFalse(pq["pq_empty"])
        doorbell = derived["decoded"]["doorbell_control"]
        self.assertTrue(doorbell["bif_drop_policy_enabled"])
        self.assertIsNone(doorbell["observed_loss"])

        rptr_map = derived["aql"]["rptr_mapping"]
        self.assertTrue(rptr_map["valid"])
        self.assertEqual(0x1BB9, rptr_map["slot_index"])
        self.assertEqual(12, rptr_map["intra_slot_dword"])
        self.assertFalse(derived["aql"]["pending"]["valid"])

    def test_raw_values_source_lines_and_wrapped_status_values(self):
        log = parse_cpc.parse_log(
            fixture("normal_target.txt"), requested_profile="gc_9_4_3"
        )
        queue = log["queues"][0]
        mqd_base = [
            item
            for item in queue["sections"]["queue_memory_registers"]
            if item["name"] == "MQD_BASE"
        ][0]
        self.assertEqual("0000000001795000", mqd_base["raw"])
        self.assertGreater(mqd_base["line"], 0)

        wave_values = queue["sections"]["queue_wave_event_registers"]
        self.assertEqual(4, len(wave_values))
        self.assertEqual([0, 1, 2, 3], [item["element_index"] for item in wave_values])

        dword = [item for item in queue["mqd"]["dwords"] if item["index_dword"] == 128][0]
        self.assertEqual("01795000", dword["raw"])
        self.assertGreater(dword["line"], 0)
        continued = [
            item
            for item in queue["mqd"]["dwords"]
            if item["index_dword"] == 135
        ][0]
        self.assertEqual("00000111", continued["raw"])
        scratch = log["global_sections"]["scratch_memory"]
        self.assertEqual([0, 1], [item["element_index"] for item in scratch])
        self.assertNotIn("scratch_memory", queue["sections"])

    def test_empty_active_queue(self):
        log = parse_cpc.parse_log(
            fixture("empty_active.txt"), requested_profile="gc_9_4_3"
        )
        queue = log["queues"][0]
        self.assertEqual(1, log["summary"]["empty_active_queue_count"])
        self.assertTrue(queue["derived"]["decoded"]["pq_control"]["pq_empty"])
        self.assertEqual(
            "EMPTY_ACTIVE_QUEUE",
            queue["findings"][0]["code"],
        )
        pending = queue["derived"]["aql"]["pending"]
        self.assertTrue(pending["valid"])
        self.assertEqual(0, pending["pending_slots_modulo_ring"])

    def test_missing_and_truncated_fields_warn_without_crashing(self):
        log = parse_cpc.parse_log(
            fixture("truncated.txt"), requested_profile="gc_9_4_3"
        )
        queue = log["queues"][0]
        codes = {warning["code"] for warning in queue["warnings"]}
        self.assertIn("TRUNCATED_WPTR", codes)
        self.assertIn("MISSING_PQ_CONTROL", codes)
        self.assertIn("MALFORMED_MQD_VALUE", codes)
        self.assertEqual(0xABC000, queue["derived"]["mqd_base"])
        self.assertIsNone(queue["derived"]["queue_size"])
        self.assertIsNone(queue["derived"]["raw_pointers"]["wptr"])

    def test_duplicate_mqd_query_reports_multiple(self):
        report = parse_cpc.build_report(
            fixture("duplicate_mqd.txt"),
            requested_profile="gc_9_4_3",
            mqd_address=0xABC000,
        )
        query = report["mqd_query"]
        self.assertEqual("multiple", query["status"])
        self.assertEqual(2, query["match_count"])
        self.assertEqual(
            {"me1.pipe0.queue0", "me1.pipe1.queue0"},
            {match["queue"]["label"] for match in query["matches"]},
        )

    def test_unknown_profile_leaves_asic_fields_raw(self):
        log = parse_cpc.parse_log(
            fixture("unknown_profile.txt"), requested_profile="auto"
        )
        queue = log["queues"][0]
        self.assertEqual("raw", log["decoder_profile"]["name"])
        self.assertIsNone(queue["derived"]["decoded"])
        self.assertIsNone(queue["derived"]["queue_size"])
        self.assertEqual(0xFFFFFFFF, parse_cpc._register_value(queue, "PQ_CONTROL")["value"])
        self.assertIn(
            "PROFILE_UNKNOWN",
            {warning["code"] for warning in queue["warnings"]},
        )

    def test_malformed_input_returns_structured_warning(self):
        log = parse_cpc.parse_log(
            fixture("malformed.txt"), requested_profile="auto"
        )
        self.assertEqual(0, log["summary"]["queue_count"])
        self.assertIn(
            "NO_CPC_STRUCTURE",
            {warning["code"] for warning in log["warnings"]},
        )

    def test_bundle_metadata_keeps_identifiers_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            cpc_dir = os.path.join(directory, "cpc")
            os.mkdir(cpc_dir)
            name = "host_20260820_160333_umr_cpc_gpu3_xcc0.txt"
            path = os.path.join(cpc_dir, name)
            shutil.copyfile(fixture("normal_target.txt"), path)
            with open(
                os.path.join(cpc_dir, "host_20260820_160333_commands.txt"),
                "w",
                encoding="utf-8",
            ) as stream:
                stream.write(
                    "output=/tmp/bundle/cpc/%s command=/opt/umr -i 1 "
                    "-vmp 0 --tool cpc -- -O no_backtrace\n" % name
                )
            with open(
                os.path.join(cpc_dir, "host_20260820_160333_kernel.txt"),
                "w",
                encoding="utf-8",
            ) as stream:
                stream.write(
                    "kernel: amdgpu 0000:0a:00.0: amdgpu: detected ip "
                    "block number 5 <gfx_v9_4_3> (gfx_v9_4_3)\n"
                )
                stream.write(
                    "kernel: [drm] Initialized amdgpu 3.64.0 20150101 "
                    "for 0000:0a:00.0 on minor 1\n"
                )

            report = parse_cpc.build_report(directory)
            context = report["logs"][0]["context"]
            self.assertEqual(3, context["filename_gpu_index"])
            self.assertEqual(1, context["umr_instance"])
            self.assertEqual("0000:0a:00.0", context["pci_bdf"])
            self.assertEqual(0, context["xcc"])
            self.assertEqual("gfx_v9_4_3", context["asic"])
            self.assertNotEqual(
                context["filename_gpu_index"], context["umr_instance"]
            )
            self.assertEqual(
                "gc_9_4_3", report["logs"][0]["decoder_profile"]["name"]
            )

    def test_stable_json_schema(self):
        report = parse_cpc.build_report(
            fixture("normal_target.txt"), requested_profile="gc_9_4_3"
        )
        encoded = json.dumps(report, sort_keys=True)
        decoded = json.loads(encoded)
        self.assertEqual("umr-cpc-report", decoded["schema_name"])
        self.assertEqual("1.0", decoded["schema_version"])
        self.assertEqual(
            {"input", "logs", "mqd_query", "schema_name", "schema_version", "summary", "tool", "warnings"},
            set(decoded),
        )


class CliTests(unittest.TestCase):
    def run_cli(self, *arguments):
        return subprocess.run(
            [sys.executable, SCRIPT] + list(arguments),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )

    def test_json_cli_and_unique_mqd_exit_zero(self):
        result = self.run_cli(
            fixture("normal_target.txt"),
            "--profile",
            "gc_9_4_3",
            "--mqd",
            "0x01795000",
            "--json",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual("unique", report["mqd_query"]["status"])
        self.assertEqual("me1.pipe0.queue2", report["mqd_query"]["matches"][0]["queue"]["label"])

    def test_zero_and_multiple_mqd_exit_codes(self):
        zero = self.run_cli(
            fixture("normal_target.txt"),
            "--profile",
            "gc_9_4_3",
            "--mqd",
            "0xdeadbeef",
        )
        self.assertEqual(parse_cpc.EXIT_NO_MQD_MATCH, zero.returncode)
        self.assertIn("zero", zero.stdout)

        multiple = self.run_cli(
            fixture("duplicate_mqd.txt"),
            "--profile",
            "gc_9_4_3",
            "--mqd",
            "0xabc000",
        )
        self.assertEqual(
            parse_cpc.EXIT_MULTIPLE_MQD_MATCHES, multiple.returncode
        )
        self.assertIn("multiple", multiple.stdout)

    def test_input_and_usage_exit_codes(self):
        missing = self.run_cli(os.path.join(FIXTURE_DIR, "does-not-exist"))
        self.assertEqual(parse_cpc.EXIT_INPUT_ERROR, missing.returncode)
        self.assertIn("error:", missing.stderr)

        bad_address = self.run_cli(
            fixture("normal_target.txt"), "--mqd", "not-an-address"
        )
        self.assertEqual(2, bad_address.returncode)

    def test_output_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "report.json")
            result = self.run_cli(
                fixture("normal_target.txt"),
                "--profile",
                "gc_9_4_3",
                "--json",
                "--output",
                output,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("", result.stdout)
            with open(output, "r", encoding="utf-8") as stream:
                self.assertEqual(
                    "umr-cpc-report", json.load(stream)["schema_name"]
                )


if __name__ == "__main__":
    unittest.main()
