#!/usr/bin/env python3
"""Focused parser, correlation, security, and HTTP tests for the dashboard."""

import copy
import http.client
import importlib.util
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
CPC_FIXTURE = HERE.parent / "cpc_parser" / "tests" / "fixtures" / "normal_target.txt"

DASHBOARD_SPEC = importlib.util.spec_from_file_location(
    "gpu_dashboard", str(HERE / "dashboard.py")
)
dashboard = importlib.util.module_from_spec(DASHBOARD_SPEC)
DASHBOARD_SPEC.loader.exec_module(dashboard)

SERVER_SPEC = importlib.util.spec_from_file_location(
    "gpu_bundle_server_dashboard_tests", str(HERE / "server.py")
)
server_module = importlib.util.module_from_spec(SERVER_SPEC)
SERVER_SPEC.loader.exec_module(server_module)


class DashboardFixture:
    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "gpu_debug_bundle_<unsafe>"
        self.root.mkdir()
        cpc = self.root / "cpc"
        cpc.mkdir()
        queue_doctor = self.root / "queue_doctor"
        queue_doctor.mkdir()
        (queue_doctor / "pending").mkdir()
        snapshot = self.root / "kfd" / "capture" / "snapshot_1"
        snapshot.mkdir(parents=True)

        cpc_name = "host_20260820_160333_umr_cpc_gpu3_xcc0.txt"
        shutil.copyfile(str(CPC_FIXTURE), str(cpc / cpc_name))
        (cpc / "host_20260820_160333_commands.txt").write_text(
            "output=/old/customer/path/cpc/{0} command=/opt/umr -i 1 "
            "-vmp 0 --tool cpc -- -O no_backtrace\n".format(cpc_name)
        )
        (cpc / "host_20260820_160333_kernel.txt").write_text(
            "kernel: amdgpu 0000:0a:00.0: detected ip block "
            "number 5 <gfx_v9_4_3> (gfx_v9_4_3)\n"
            "kernel: [drm] Initialized amdgpu 3.64.0 20150101 "
            "for 0000:0a:00.0 on minor 1\n"
        )
        (self.root / "sdma_registers.txt").write_text(
            "# GPU mapping\n"
            "# gpu=2 amd_smi_gpu=0 pci_bdf=0000:0a:00.0\n"
        )
        (snapshot / "rls.txt").write_text(
            "Node 2, gpu_id f7bc:\n  00000000: 01795000 00000000\n"
        )
        (snapshot / "hqds.txt").write_text(
            "Node 2, gpu_id f7bc:\n"
            " Inst 0, CP Pipe 0, Queue 2\n"
            "  0000c914: 01795000 00000000 00000001 00000303 \n"
        )
        target = {
            "pid": 2056320,
            "target_gpus": [
                {
                    "amd_smi_id": 0,
                    "gpu_id": 63420,
                    "node_id": 2,
                    "pci_bdf": "0000:0a:00.0",
                    "render_minor": 128,
                    "kfd_queue_ids": [0],
                }
            ],
        }
        (queue_doctor / "target-gpus.json").write_text(json.dumps(target))
        (queue_doctor / "summary.txt").write_text(
            "Process: PID 2056320 (MultiThreadTest)\n"
            "  umr_queue=0 type=compute(1) rptr=0x10 wptr=0x12 "
            "state=pending kfd_gpu_id=63420\n"
            "  umr_queue=0 type=compute(1) rptr=0x10 wptr=0x12 "
            "state=pending kfd_gpu_id=63420\n"
            "  [WARNING] QUEUE_NO_PROGRESS (queue=0): stable pointers\n"
        )
        (queue_doctor / "snapshot-01-list-uq.txt").write_text(
            "Client #: comm=[MultiThreadTest] tgid=2056320 type=kfd\n"
            "** queue=0 type=1 mqd_gpu_addr=0x1795000 "
            "rptr=0x10 wptr=0x12\n"
        )
        (queue_doctor / "pending" / "snapshot-01-queue-0-compute.txt").write_text(
            "$ umr --vm-partition 0 --dump-uq\n"
            "Opcode 0x2 [HSA_KERNEL_DISPATCH]\n"
            "Opcode 0x1 [HSA_INVALID]\n"
        )
        (self.root / "system_info.txt").write_text(
            "collected_at=2026-08-20T16:00:37+08:00\n"
            "hostname=host<script>alert(1)</script>\n"
            "kernel=5.10-test\n"
        )
        (self.root / "summary.txt").write_text(
            "cpc_dump PASS 0 elapsed=1s\n"
            "queue_doctor PASS 0 elapsed=1s\n"
            "completed_at=2026-08-20T16:06:55+08:00\n"
        )

    def cleanup(self):
        self.temporary.cleanup()


class DashboardAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.fixture = DashboardFixture()

    def tearDown(self):
        self.fixture.cleanup()

    def test_known_target_metadata_and_exact_mqd_correlation(self):
        model = dashboard.analyze_bundle(str(self.fixture.root), force=True)
        self.assertEqual(1, model["overview"]["gpu_count"])
        self.assertEqual(2, model["overview"]["pending_total"])
        matches = [
            row
            for row in model["queues"]
            if row["mqd_base"] == 0x01795000
            and row["pci_bdf"] == "0000:0a:00.0"
            and row["xcc"] == 0
        ]
        self.assertEqual(1, len(matches))
        row = matches[0]
        self.assertEqual("me1.pipe0.queue2", row["label"])
        self.assertEqual(3, row["filename_gpu_index"])
        self.assertEqual(1, row["umr_instance"])
        self.assertEqual("exact", row["queue_doctor_join"]["status"])
        self.assertEqual("exact", row["kfd_hqd_join"]["status"])
        self.assertEqual("stall-candidate", row["status"])
        gpu = model["gpus"][0]
        self.assertEqual(0, gpu["amd_smi_id"])
        self.assertEqual(63420, gpu["gpu_id"])
        self.assertEqual(2, gpu["node_id"])
        self.assertEqual("renderD128", gpu["render_node"])
        self.assertEqual([3], gpu["filename_gpu_indices"])

    def test_exact_join_duplicate_unmatched_scope_and_status_classification(self):
        row = {"mqd_base": 0xABC, "pci_bdf": "0000:0a:00.0", "xcc": 0}
        candidates = [
            {"mqd_base": 0xABC, "pci_bdf": "0000:0a:00.0", "xcc": 0},
            {"mqd_base": 0xABC, "pci_bdf": "0000:0a:00.0", "xcc": 0},
            {"mqd_base": 0xABC, "pci_bdf": "0000:80:00.0", "xcc": 0},
            {"mqd_base": 0xABC, "pci_bdf": "0000:0a:00.0", "xcc": 1},
        ]
        self.assertEqual(
            "ambiguous", dashboard.exact_mqd_join(row, candidates)["status"]
        )
        self.assertEqual(
            "exact", dashboard.exact_mqd_join(row, candidates[:1])["status"]
        )
        self.assertEqual(
            "unmatched",
            dashboard.exact_mqd_join(
                dict(row, mqd_base=0xDEF), candidates
            )["status"],
        )
        self.assertEqual(
            "inactive-stale",
            dashboard.classify_cpc_queue({"derived": {"active": {"is_active": False}}}),
        )
        self.assertEqual(
            "unknown",
            dashboard.classify_cpc_queue({"derived": {"active": {"is_active": True}}}),
        )
        self.assertEqual(
            "empty-active",
            dashboard.classify_cpc_queue(
                {
                    "derived": {
                        "active": {"is_active": True},
                        "decoded": {"pq_control": {"pq_empty": True}},
                    }
                }
            ),
        )

    def test_html_escaping_cache_and_safe_source_urls(self):
        first = dashboard.analyze_bundle(str(self.fixture.root), force=True)
        cached = dashboard.analyze_bundle(str(self.fixture.root))
        self.assertIs(first, cached)
        model = copy.deepcopy(first)
        model["warnings"].append(
            {"code": "<img src=x>", "message": "<script>alert(2)</script>"}
        )
        rendered = dashboard.render_dashboard(
            model, '<footer class="release-footer">v-test</footer>'
        )
        self.assertIn("&lt;unsafe&gt;", rendered)
        self.assertIn("host&lt;script&gt;", rendered)
        self.assertIn("&lt;script&gt;alert(2)&lt;/script&gt;", rendered)
        self.assertNotIn("<script>alert(2)</script>", rendered)
        self.assertIsNone(dashboard.source_url("../outside.txt"))
        self.assertEqual(
            "/queue_doctor/summary.txt#L4",
            dashboard.source_url("queue_doctor/summary.txt", 4),
        )

    def test_missing_optional_sources_report_empty_states(self):
        shutil.rmtree(str(self.fixture.root / "queue_doctor"))
        model = dashboard.analyze_bundle(str(self.fixture.root), force=True)
        self.assertTrue(
            any(
                warning["code"] == "TARGET_METADATA_ABSENT"
                for warning in model["warnings"]
            )
        )
        queue_doctor = [
            panel for panel in model["source_panels"] if panel["name"] == "Queue Doctor"
        ][0]
        self.assertEqual("absent", queue_doctor["status"])
        self.assertEqual(0, model["overview"]["pending_total"])


class DashboardHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = DashboardFixture()
        handler = server_module.make_handler(str(self.fixture.root))
        handler.log_message = lambda *args: None
        self.httpd = server_module.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.fixture.cleanup()

    def request(self, path, method="GET"):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.httpd.server_address[1], timeout=10
        )
        connection.request(method, path)
        response = connection.getresponse()
        body = response.read()
        headers = dict(response.getheaders())
        status = response.status
        connection.close()
        return status, headers, body

    def test_dashboard_control_route_themes_footer_refresh_and_head(self):
        status, _, body = self.request("/")
        self.assertEqual(200, status)
        self.assertIn('href="/__dashboard__/"', body.decode())

        status, headers, body = self.request("/__dashboard__")
        self.assertEqual(301, status)
        self.assertEqual("/__dashboard__/", headers["Location"])
        self.assertEqual(b"", body)

        status, headers, body = self.request("/__dashboard__/?refresh=1")
        page = body.decode("utf-8")
        self.assertEqual(200, status)
        self.assertEqual("text/html; charset=utf-8", headers["Content-Type"])
        self.assertIn("Queue Analysis Dashboard", page)
        self.assertIn("me1.pipe0.queue2", page)
        self.assertIn("stall candidate", page)
        self.assertIn("AMD-SMI GPU 0", page)
        self.assertIn('data-theme="amber"', page)
        self.assertIn('<option value="amber">暗黄</option>', page)
        self.assertIn(server_module.release_label(), page)
        self.assertIn("non-atomic", page)
        self.assertNotIn(str(self.fixture.root), page)

        status, headers, body = self.request("/__dashboard__/", method="HEAD")
        self.assertEqual(200, status)
        self.assertGreater(int(headers["Content-Length"]), 0)
        self.assertEqual(b"", body)


if __name__ == "__main__":
    unittest.main()
