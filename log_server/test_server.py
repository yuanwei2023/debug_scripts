#!/usr/bin/env python3
"""Focused tests for the GPU debug bundle web server."""

import errno
import http.client
import importlib.util
import io
import os
import tempfile
import threading
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("server.py")
SPEC = importlib.util.spec_from_file_location("gpu_bundle_server", str(MODULE_PATH))
server_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server_module)


class GPUBundleServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.container = Path(self.temporary.name)
        self.root = self.container / "bundle"
        self.root.mkdir()
        self.nested = self.root / "Alpha Dir"
        self.nested.mkdir()
        (self.nested / "inside.txt").write_bytes(b"nested content\n")
        (self.root / "aardvark.txt").write_bytes(b"a" * 1536)
        (self.root / "z-last.json").write_bytes(b'{"ok": true}\n')
        (self.root / "r\u00e9sum\u00e9 & notes.txt").write_bytes(
            "Unicode works\n".encode("utf-8")
        )
        (self.root / "<unsafe>.txt").write_bytes(b"escaped\n")
        (self.root / "diagnostic.log").write_bytes(
            b"2026-08-21 13:35:01 ERROR queue=0xDEADBEEF count=42 "
            b"path=/tmp/gpu/debug.txt\n"
            b"WARNING retry=3 INFO DEBUG SUCCESS PASS OK\n"
            b"# comment with FATAL 0xBAD\n"
            b'<script>alert("owned")</script> FAIL\n'
            b"INFO invalid_utf8=\xff\n"
        )
        (self.root / "large.log").write_text(
            "".join("INFO line={} status=OK\n".format(index) for index in range(1, 2006))
        )
        self.modified = 1_700_000_000
        os.utime(str(self.root / "aardvark.txt"), (self.modified, self.modified))
        (self.container / "outside.txt").write_bytes(b"outside")
        try:
            os.symlink(
                str(self.container / "outside.txt"),
                str(self.root / "outside-link.txt"),
            )
            self.has_symlinks = True
        except (OSError, NotImplementedError):
            self.has_symlinks = False

        handler = server_module.make_handler(str(self.root))
        handler.log_message = lambda *args: None
        self.httpd = server_module.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, path, method="GET"):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.httpd.server_address[1], timeout=5
        )
        connection.request(method, path)
        response = connection.getresponse()
        body = response.read()
        headers = dict(response.getheaders())
        status = response.status
        connection.close()
        return status, headers, body

    def test_listing_has_metadata_escaping_and_directories_first(self):
        status, headers, body_bytes = self.request("/")
        body = body_bytes.decode("utf-8")

        self.assertEqual(status, 200)
        self.assertEqual(
            headers["Content-Length"], str(len(body_bytes))
        )
        self.assertIn("text/html; charset=utf-8", headers["Content-Type"])
        self.assertLess(body.index("Alpha Dir"), body.index("aardvark.txt"))
        self.assertLess(body.index("aardvark.txt"), body.index("z-last.json"))
        self.assertIn("1.5 KiB", body)
        self.assertIn(server_module.format_modified(self.modified), body)
        self.assertIn("&lt;unsafe&gt;.txt", body)
        self.assertNotIn("<unsafe>.txt", body)
        self.assertNotIn(str(self.container), body)
        if self.has_symlinks:
            self.assertNotIn("outside-link.txt", body)

    def test_navigation_back_button_breadcrumbs_and_three_theme_markup(self):
        root_status, _, root_body = self.request("/")
        nested_status, _, nested_body = self.request("/Alpha%20Dir/")
        root_html = root_body.decode("utf-8")
        nested_html = nested_body.decode("utf-8")

        self.assertEqual(root_status, 200)
        self.assertEqual(nested_status, 200)
        self.assertIn('aria-disabled="true">\u2190 Back', root_html)
        self.assertIn('class="back" href="/">\u2190 Back</a>', nested_html)
        self.assertIn("Current path: /Alpha Dir", nested_html)
        self.assertIn('aria-current="page">Alpha Dir</span>', nested_html)
        self.assertIn("localStorage.getItem('gpu-bundle-theme')", root_html)
        self.assertIn("localStorage.setItem('gpu-bundle-theme'", root_html)
        self.assertIn("supported = ['light', 'dark', 'amber']", root_html)
        self.assertIn('id="theme-select"', root_html)
        self.assertIn('<option value="light">Light</option>', root_html)
        self.assertIn('<option value="dark">Dark</option>', root_html)
        self.assertIn('<option value="amber">\u6697\u9ec4</option>', root_html)
        self.assertIn(':root[data-theme="dark"]', root_html)
        self.assertIn(':root[data-theme="amber"]', root_html)
        self.assertIn("--accent:#f4c95d", root_html)
        self.assertIn("--accent-contrast:#211600", root_html)
        self.assertIn("a:focus-visible,select:focus-visible", root_html)
        self.assertIn('class="badge">Directory</span>', root_html)
        self.assertIn("prefers-color-scheme: dark", root_html)
        self.assertNotIn('id="theme-toggle"', root_html)

    def test_text_file_opens_viewer_with_raw_and_download_actions(self):
        status, headers, body = self.request("/r%C3%A9sum%C3%A9%20%26%20notes.txt")
        self.assertEqual(status, 200)
        viewer = body.decode("utf-8")
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn("Highlighted log viewer", viewer)
        self.assertIn(
            'href="/r%C3%A9sum%C3%A9%20%26%20notes.txt?raw=1">Raw</a>',
            viewer,
        )
        self.assertIn(
            'href="/r%C3%A9sum%C3%A9%20%26%20notes.txt?download=1">Download</a>',
            viewer,
        )
        self.assertNotIn("Content-Disposition", headers)

        status, headers, body = self.request(
            "/r%C3%A9sum%C3%A9%20%26%20notes.txt?raw=1"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, "Unicode works\n".encode("utf-8"))
        self.assertEqual(headers["Content-Type"], "text/plain")
        self.assertNotIn("Content-Disposition", headers)

        status, headers, body = self.request(
            "/r%C3%A9sum%C3%A9%20%26%20notes.txt?download=1"
        )
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Disposition"].startswith("attachment;"))
        self.assertIn(
            "filename*=UTF-8''r%C3%A9sum%C3%A9%20%26%20notes.txt",
            headers["Content-Disposition"],
        )
        self.assertEqual(headers["Content-Length"], str(len(body)))

    def test_log_highlighting_line_numbers_escaping_and_replacement(self):
        status, headers, body = self.request("/diagnostic.log")
        viewer = body.decode("utf-8")

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        for token_class in (
            "tok-timestamp",
            "tok-error",
            "tok-warning",
            "tok-success",
            "tok-info",
            "tok-hex",
            "tok-number",
            "tok-key",
            "tok-path",
            "tok-comment",
        ):
            self.assertIn('class="{}"'.format(token_class), viewer)
        self.assertIn('class="line-number" href="#L1"', viewer)
        self.assertIn('id="L5"', viewer)
        self.assertIn("&lt;script&gt;", viewer)
        self.assertNotIn('<script>alert("owned")</script>', viewer)
        self.assertIn("\ufffd", viewer)
        self.assertIn(':root[data-theme="amber"]', viewer)
        self.assertIn('<option value="amber">\u6697\u9ec4</option>', viewer)

    def test_large_text_preview_is_bounded_with_complete_file_links(self):
        status, _, body = self.request("/large.log")
        viewer = body.decode("utf-8")

        self.assertEqual(status, 200)
        self.assertIn("<strong>Preview limited.</strong>", viewer)
        self.assertIn("2,000-line preview limit reached", viewer)
        self.assertIn('id="L2000"', viewer)
        self.assertNotIn('id="L2001"', viewer)
        self.assertIn('href="/large.log?raw=1">Raw</a>', viewer)
        self.assertIn('href="/large.log?download=1">Download</a>', viewer)

        preview = server_module.read_text_preview(
            io.BytesIO(b"x" * (server_module.MAX_PREVIEW_BYTES + 1))
        )
        self.assertTrue(preview.byte_limited)
        self.assertTrue(preview.truncated)
        self.assertEqual(preview.clipped_lines, 1)
        self.assertLessEqual(
            len(preview.lines[0]), server_module.MAX_PREVIEW_LINE_CHARS
        )

    def test_non_text_file_handling_is_unchanged(self):
        status, headers, body = self.request("/z-last.json")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(body, b'{"ok": true}\n')
        self.assertNotIn("Content-Disposition", headers)

    def test_release_metadata_is_shared_across_all_html_pages(self):
        expected = "v{} \u00b7 Released {}".format(
            server_module.SERVER_SEMANTIC_VERSION,
            server_module.SERVER_RELEASE_TIMESTAMP,
        )
        self.assertEqual(server_module.release_label(), expected)

        for path, expected_status in (
            ("/", 200),
            ("/diagnostic.log", 200),
            ("/missing.txt", 404),
        ):
            status, _, body = self.request(path)
            page = body.decode("utf-8")
            self.assertEqual(status, expected_status)
            self.assertEqual(page.count(expected), 1, path)
            self.assertIn('class="release-footer"', page)
        self.assertIn("UTC+8", expected)

    def test_url_quoting_round_trip_and_directory_redirect(self):
        status, _, body = self.request("/")
        listing = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn(
            'href="/r%C3%A9sum%C3%A9%20%26%20notes.txt"', listing
        )
        self.assertIn('href="/Alpha%20Dir/"', listing)

        status, headers, body = self.request("/Alpha%20Dir")
        self.assertEqual(status, 301)
        self.assertEqual(headers["Location"], "/Alpha%20Dir/")
        self.assertEqual(body, b"")

    def test_traversal_malformed_urls_symlinks_and_missing_files_fail(self):
        attempts = (
            "/%2e%2e/outside.txt",
            "/Alpha%20Dir/%2E%2E/%2E%2E/outside.txt",
            "/%2F%2Fetc/passwd",
            "/missing.txt",
        )
        for path in attempts:
            status, _, body = self.request(path)
            self.assertIn(status, (400, 404), path)
            self.assertNotIn(str(self.container).encode("utf-8"), body)

        status, _, _ = self.request("/bad%ZZname")
        self.assertEqual(status, 400)
        if self.has_symlinks:
            status, _, body = self.request("/outside-link.txt")
            self.assertEqual(status, 404)
            self.assertNotIn(b"outside", body)

    def test_head_has_file_headers_without_body(self):
        status, headers, body = self.request("/aardvark.txt?raw=1", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Length"], "1536")
        self.assertEqual(body, b"")

    def test_permission_error_translation(self):
        with self.assertRaises(server_module.RequestPathError) as context:
            server_module.raise_request_error(OSError(errno.EACCES, "denied"))
        self.assertEqual(context.exception.status, 403)
        self.assertEqual(context.exception.message, "Permission denied")


if __name__ == "__main__":
    unittest.main()
