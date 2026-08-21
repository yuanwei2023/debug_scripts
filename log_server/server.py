#!/usr/bin/env python3
"""Dependency-free, safely rooted web browser for GPU debug bundles."""

import argparse
import datetime
import errno
import html
import http.server
import mimetypes
import os
import re
import shutil
import socketserver
import stat
import sys
from email.utils import formatdate
from urllib.parse import parse_qs, quote, unquote_to_bytes

SERVER_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
if SERVER_DIRECTORY not in sys.path:
    sys.path.insert(0, SERVER_DIRECTORY)
import dashboard
import full_ring_viewer


MALFORMED_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
SERVER_SEMANTIC_VERSION = "2.1.1"
SERVER_RELEASE_TIMESTAMP = "2026-08-21 16:49 UTC+8"
TEXT_VIEW_EXTENSIONS = (".txt", ".log")
MAX_PREVIEW_BYTES = 256 * 1024
MAX_PREVIEW_LINES = 2000
MAX_PREVIEW_LINE_CHARS = 16384
MAX_HIGHLIGHTS_PER_LINE = 200

TOKEN_PATTERN = re.compile(
    r"(?P<timestamp>\b\d{4}-\d{2}-\d{2}[T ]"
    r"[0-2]\d:[0-5]\d:[0-5]\d(?:[.,]\d+)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?\b"
    r"|\b[0-2]\d:[0-5]\d:[0-5]\d(?:[.,]\d+)?\b)"
    r"|(?P<error>\b(?:ERROR|FAIL(?:ED|URE)?|FATAL)\b)"
    r"|(?P<warning>\b(?:WARNING|WARN)\b)"
    r"|(?P<success>\b(?:SUCCESS|PASS(?:ED)?|OK)\b)"
    r"|(?P<info>\b(?:INFO|DEBUG)\b)"
    r"|(?P<hex>\b0[xX][0-9A-Fa-f]+\b)"
    r"|(?P<path>/(?:[A-Za-z0-9_.+@~-]+/)*[A-Za-z0-9_.+@~-]+)"
    r"|(?P<key>\b[A-Za-z_][A-Za-z0-9_.-]*(?=\s*(?:=|:)))"
    r"|(?P<number>(?<![\w.])[-+]?\d+(?:\.\d+)?(?![\w.]))",
    re.IGNORECASE,
)
COMMENT_PATTERN = re.compile(r"^(\s*)(#|//|;)(.*)$")


class RequestPathError(Exception):
    """An invalid or inaccessible request path."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class DirectoryEntry:
    """Display metadata for one safe directory entry."""

    def __init__(self, name, is_directory, size, modified):
        self.name = name
        self.is_directory = is_directory
        self.size = size
        self.modified = modified


class TextPreview:
    """Bounded decoded text prepared for safe HTML rendering."""

    def __init__(
        self,
        lines,
        byte_limited=False,
        line_limited=False,
        clipped_lines=0,
        line_numbers=None,
        pointer_roles=None,
        mode="default",
        pointer_error=None,
        scan_limited=False,
        window=None,
    ):
        self.lines = lines
        self.byte_limited = byte_limited
        self.line_limited = line_limited
        self.clipped_lines = clipped_lines
        self.line_numbers = (
            list(line_numbers)
            if line_numbers is not None
            else list(range(1, len(lines) + 1))
        )
        self.pointer_roles = pointer_roles or {}
        self.mode = mode
        self.pointer_error = pointer_error
        self.scan_limited = scan_limited
        self.window = window

    @property
    def truncated(self):
        return self.byte_limited or self.line_limited or self.clipped_lines > 0


def is_viewable_text(filename):
    """Return whether a filename should open in the integrated viewer."""
    return filename.casefold().endswith(TEXT_VIEW_EXTENSIONS)


def read_text_preview(stream):
    """Read a bounded UTF-8 preview, replacing invalid byte sequences."""
    data = stream.read(MAX_PREVIEW_BYTES + 1)
    byte_limited = len(data) > MAX_PREVIEW_BYTES
    text = data[:MAX_PREVIEW_BYTES].decode("utf-8", errors="replace")
    lines = text.splitlines()
    if not lines:
        lines = [""]
    line_limited = len(lines) > MAX_PREVIEW_LINES
    lines = lines[:MAX_PREVIEW_LINES]

    clipped_lines = 0
    bounded_lines = []
    for line in lines:
        if len(line) > MAX_PREVIEW_LINE_CHARS:
            line = line[:MAX_PREVIEW_LINE_CHARS]
            clipped_lines += 1
        bounded_lines.append(line)
    return TextPreview(
        bounded_lines,
        byte_limited=byte_limited,
        line_limited=line_limited,
        clipped_lines=clipped_lines,
    )


def highlight_log_line(line):
    """Escape a log line and add bounded token spans without trusting content."""
    comment = COMMENT_PATTERN.match(line)
    if comment:
        return "{}<span class=\"tok-comment\">{}</span>".format(
            html.escape(comment.group(1)),
            html.escape(comment.group(2) + comment.group(3)),
        )

    pieces = []
    position = 0
    for index, match in enumerate(TOKEN_PATTERN.finditer(line)):
        if index >= MAX_HIGHLIGHTS_PER_LINE:
            break
        pieces.append(html.escape(line[position : match.start()]))
        token_class = "tok-" + match.lastgroup
        pieces.append(
            '<span class="{}">{}</span>'.format(
                token_class, html.escape(match.group(0))
            )
        )
        position = match.end()
    pieces.append(html.escape(line[position:]))
    return "".join(pieces)


def release_label():
    """Return static release metadata, never the process start time."""
    return "v{} \u00b7 Released {}".format(
        SERVER_SEMANTIC_VERSION, SERVER_RELEASE_TIMESTAMP
    )


def release_footer():
    """Return the shared release footer used by every generated HTML page."""
    return '<footer class="release-footer">{}</footer>'.format(
        html.escape(release_label())
    )


def human_size(size):
    """Return a compact IEC byte-size label."""
    if size is None:
        return "\u2014"
    if size < 1024:
        return "{} B".format(size)
    value = float(size)
    units = ("KiB", "MiB", "GiB", "TiB", "PiB")
    for unit in units:
        value /= 1024.0
        if value < 1024.0 or unit == units[-1]:
            if value >= 10:
                return "{:.0f} {}".format(value, unit)
            return "{:.1f} {}".format(value, unit)
    return "{} B".format(size)


def format_modified(timestamp):
    """Format a timestamp in the server's local time."""
    return datetime.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def encode_url_path(parts, directory=False):
    """Quote logical path components for use in an origin-form URL."""
    path = "/" + "/".join(quote(part, safe="") for part in parts)
    if directory and path != "/" and not path.endswith("/"):
        path += "/"
    return path


def decode_request_path(raw_target):
    """Decode and validate an HTTP origin-form request path."""
    raw_path = raw_target.partition("?")[0]
    if not raw_path.startswith("/") or raw_path.startswith("//"):
        raise RequestPathError(400, "Invalid request path")
    if MALFORMED_ESCAPE.search(raw_path):
        raise RequestPathError(400, "Malformed URL escape")
    try:
        decoded = unquote_to_bytes(raw_path).decode("utf-8")
    except UnicodeDecodeError:
        raise RequestPathError(400, "Path is not valid UTF-8")
    if "\x00" in decoded or "\\" in decoded or decoded.startswith("//"):
        raise RequestPathError(400, "Invalid request path")

    raw_parts = decoded.split("/")
    if any(part == "" for part in raw_parts[1:-1]):
        raise RequestPathError(400, "Invalid empty path component")
    parts = tuple(part for part in raw_parts if part)
    if any(part in (".", "..") for part in parts):
        raise RequestPathError(404, "Path not found")
    return parts


def raise_request_error(error):
    """Translate filesystem errors to non-revealing HTTP errors."""
    if error.errno in (errno.EACCES, errno.EPERM):
        raise RequestPathError(403, "Permission denied")
    raise RequestPathError(404, "Path not found")


def list_directory_entries(path):
    """Return safe regular files and directories with display metadata."""
    entries = []
    try:
        iterator = os.scandir(path)
        with iterator:
            for item in iterator:
                try:
                    info = item.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISLNK(info.st_mode):
                    continue
                is_directory = stat.S_ISDIR(info.st_mode)
                if not is_directory and not stat.S_ISREG(info.st_mode):
                    continue
                entries.append(
                    DirectoryEntry(
                        item.name,
                        is_directory,
                        None if is_directory else info.st_size,
                        info.st_mtime,
                    )
                )
    except OSError as error:
        raise_request_error(error)

    entries.sort(
        key=lambda item: (
            not item.is_directory,
            item.name.casefold(),
            item.name,
        )
    )
    return entries


def content_disposition(filename):
    """Build a safe attachment header with UTF-8 filename support."""
    fallback = filename.encode("ascii", "replace").decode("ascii")
    fallback = "".join(
        "_" if ord(char) < 32 or ord(char) == 127 else char for char in fallback
    )
    fallback = fallback.replace("\\", "_").replace('"', "_")
    return "attachment; filename=\"{}\"; filename*=UTF-8''{}".format(
        fallback, quote(filename, safe="")
    )


class GPUBundleRequestHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler rooted at one configured bundle directory."""

    server_version = "GPUBundleBrowser/{}".format(SERVER_SEMANTIC_VERSION)
    sys_version = ""
    root_directory = None

    def version_string(self):
        return self.server_version

    def do_GET(self):
        self._handle_request(send_body=True)

    def do_HEAD(self):
        self._handle_request(send_body=False)

    def _handle_request(self, send_body):
        try:
            raw_path = self.path.partition("?")[0]
            if raw_path == "/__dashboard__":
                self.send_response(301)
                self.send_header("Location", "/__dashboard__/")
                self.send_header("Content-Length", "0")
                self._send_security_headers()
                self.end_headers()
                return
            if raw_path == "/__dashboard__/":
                if not dashboard.is_gpu_debug_bundle(self.root_directory):
                    raise RequestPathError(404, "Dashboard unavailable")
                query = self.path.partition("?")[2]
                parameters = parse_qs(query, keep_blank_values=True)
                self._serve_dashboard("refresh" in parameters, send_body)
                return
            parts = decode_request_path(self.path)
            path, path_info = self._resolve_path(parts)
            if stat.S_ISDIR(path_info.st_mode):
                self._serve_directory(path, parts, send_body)
            elif stat.S_ISREG(path_info.st_mode):
                query = self.path.partition("?")[2]
                parameters = parse_qs(query, keep_blank_values=True)
                download = "download" in parameters
                raw = "raw" in parameters
                if is_viewable_text(parts[-1]) and not download and not raw:
                    self._serve_text_viewer(path, parts, send_body)
                else:
                    self._serve_file(path, parts[-1], download, send_body)
            else:
                raise RequestPathError(404, "Path not found")
        except RequestPathError as error:
            self._send_error_page(error.status, error.message, send_body)
        except OSError as error:
            try:
                raise_request_error(error)
            except RequestPathError as request_error:
                self._send_error_page(
                    request_error.status, request_error.message, send_body
                )

    def _serve_dashboard(self, force_refresh, send_body):
        try:
            model = dashboard.analyze_bundle(
                self.root_directory, force=force_refresh
            )
            rendered = dashboard.render_dashboard(model, release_footer())
        except ValueError:
            raise RequestPathError(404, "Dashboard unavailable")
        except Exception:
            raise RequestPathError(500, "Dashboard analysis failed")
        body = rendered.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers()
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def _resolve_path(self, parts):
        candidate = os.path.abspath(os.path.join(self.root_directory, *parts))
        try:
            inside_root = (
                os.path.commonpath((self.root_directory, candidate))
                == self.root_directory
            )
        except ValueError:
            inside_root = False
        if not inside_root:
            raise RequestPathError(404, "Path not found")

        current = self.root_directory
        try:
            path_info = os.lstat(current)
            if stat.S_ISLNK(path_info.st_mode):
                raise RequestPathError(404, "Path not found")
            for part in parts:
                current = os.path.join(current, part)
                path_info = os.lstat(current)
                if stat.S_ISLNK(path_info.st_mode):
                    raise RequestPathError(404, "Path not found")
        except OSError as error:
            raise_request_error(error)

        real_path = os.path.realpath(current)
        try:
            inside_root = (
                os.path.commonpath((self.root_directory, real_path))
                == self.root_directory
            )
        except ValueError:
            inside_root = False
        if not inside_root:
            raise RequestPathError(404, "Path not found")
        return current, path_info

    def _serve_directory(self, path, parts, send_body):
        raw_path = self.path.partition("?")[0]
        if parts and not raw_path.endswith("/"):
            self.send_response(301)
            self.send_header("Location", encode_url_path(parts, directory=True))
            self.send_header("Content-Length", "0")
            self._send_security_headers()
            self.end_headers()
            return

        body = self._render_directory(parts, list_directory_entries(path)).encode(
            "utf-8"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers()
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def _serve_file(self, path, filename, download, send_body):
        stream = self._open_regular_file(path)
        with stream:
            info = os.fstat(stream.fileno())
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(info.st_size))
            self.send_header("Last-Modified", formatdate(info.st_mtime, usegmt=True))
            if download:
                self.send_header("Content-Disposition", content_disposition(filename))
            self._send_security_headers()
            self.end_headers()
            if send_body:
                shutil.copyfileobj(stream, self.wfile)

    def _serve_text_viewer(self, path, parts, send_body):
        stream = self._open_regular_file(path)
        with stream:
            info = os.fstat(stream.fileno())
            relative = "/".join(parts)
            full_ring = full_ring_viewer.inspect_full_ring(
                stream, self.root_directory, relative
            )
            query = self.path.partition("?")[2]
            parameters = parse_qs(query, keep_blank_values=True)
            pointer_mode = (parameters.get("pointer") or [None])[0]
            if pointer_mode not in ("rptr", "wptr"):
                pointer_mode = None
            if full_ring is not None and pointer_mode is not None:
                context_text = (parameters.get("context") or [""])[0]
                try:
                    context = int(context_text, 10)
                except (TypeError, ValueError):
                    context = full_ring_viewer.DEFAULT_SLOT_CONTEXT
                if context not in full_ring_viewer.ALLOWED_SLOT_CONTEXTS:
                    context = full_ring_viewer.DEFAULT_SLOT_CONTEXT
                slot_text = (parameters.get("slot") or [""])[0]
                try:
                    center_slot = int(slot_text, 10)
                except (TypeError, ValueError):
                    center_slot = None
                if (
                    center_slot is not None
                    and not 0 <= center_slot < full_ring.get("slot_count", 0)
                ):
                    center_slot = None
                targeted = full_ring_viewer.read_slot_window(
                    stream,
                    full_ring,
                    pointer_mode,
                    center_slot=center_slot,
                    context=context,
                )
                if targeted["available"]:
                    preview = TextPreview(
                        targeted["lines"],
                        line_numbers=targeted["line_numbers"],
                        pointer_roles=targeted["roles"],
                        mode="slot-window",
                        scan_limited=targeted["scan_limited"],
                        window=targeted["window"],
                    )
                else:
                    preview = read_text_preview(stream)
                    preview.pointer_error = targeted["reason"]
                    preview.scan_limited = targeted["scan_limited"]
            else:
                preview = read_text_preview(stream)
            if full_ring is not None and preview.mode == "default":
                preview.pointer_roles = full_ring_viewer.pointer_roles_for_lines(
                    preview.lines, preview.line_numbers, full_ring
                )
        body = self._render_text_viewer(
            parts, info, preview, full_ring, pointer_mode
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Last-Modified", formatdate(info.st_mtime, usegmt=True))
        self._send_security_headers()
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def _open_regular_file(self, path):
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
            stream = os.fdopen(descriptor, "rb")
        except OSError as error:
            raise_request_error(error)

        try:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise RequestPathError(404, "Path not found")
        except Exception:
            stream.close()
            raise
        return stream

    def _send_security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'")

    def _send_error_page(self, status, message, send_body):
        reason = http.server.BaseHTTPRequestHandler.responses.get(
            status, ("Error", "")
        )[0]
        body = (
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>{status} {reason}</title><script>(()=>{{"
            "const supported=['light','dark','amber'];"
            "let theme=matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';"
            "try{{const saved=localStorage.getItem('gpu-bundle-theme');"
            "if(supported.includes(saved))theme=saved;}}catch(_){{}}"
            "document.documentElement.dataset.theme=theme;}})();</script><style>"
            ":root{{--bg:#f5f7fb;--surface:#fff;--text:#263248;--muted:#65738a;"
            "--line:#dfe5ef;--accent:#2859c5}}"
            ":root[data-theme=\"dark\"]{{--bg:#0f1520;--surface:#171f2d;"
            "--text:#edf2fb;--muted:#aab6c8;--line:#303c50;--accent:#8eafff}}"
            ":root[data-theme=\"amber\"]{{--bg:#15120c;--surface:#211b11;"
            "--text:#fff4d0;--muted:#d2bb7a;--line:#5d4822;--accent:#f4c95d}}"
            "body{{min-height:100vh;margin:0;padding:2rem;display:flex;"
            "flex-direction:column;font:16px system-ui,sans-serif;color:var(--text);"
            "background:var(--bg)}}main{{width:min(42rem,100%);margin:8vh auto;"
            "flex:1}}a{{color:var(--accent)}}"
            ".release-footer{{border-top:1px solid var(--line);padding:14px 4px 0;"
            "color:var(--muted);font-size:12px;text-align:center}}"
            "</style></head><body><main><h1>{status} {reason}</h1>"
            "<p>{message}</p><p><a href=\"/\">Return to bundle</a></p></main>"
            "{footer}</body></html>"
        ).format(
            status=status,
            reason=html.escape(reason),
            message=html.escape(message),
            footer=release_footer(),
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers()
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def _render_full_ring_panel(self, metadata, file_url, preview, pointer_mode):
        def value(text, fallback="Unavailable"):
            return html.escape(str(text)) if text is not None else fallback

        def pointer_card(role):
            label = role.upper()
            pointer = metadata.get(role + "_view")
            if pointer is None:
                return (
                    '<article class="pointer-card unavailable"><h3>{}</h3>'
                    '<p>Pointer metadata unavailable</p></article>'
                ).format(label)
            return (
                '<article class="pointer-card {role}"><h3>{label}</h3>'
                '<dl><div><dt>Logical index</dt><dd>{logical}</dd></div>'
                '<div><dt>Physical slot</dt><dd>{slot}</dd></div>'
                '<div><dt>Byte offset</dt><dd>0x{offset:x} ({offset_dec:,})</dd></div>'
                '<div><dt>Ring address</dt><dd>0x{address:x}</dd></div></dl>'
                '<p class="pointer-meaning">{meaning}</p></article>'
            ).format(
                role=role,
                label=label,
                logical=pointer["logical"],
                slot=pointer["slot"],
                offset=pointer["byte_offset"],
                offset_dec=pointer["byte_offset"],
                address=pointer["address"],
                meaning=(
                    "First packet the consumer may process."
                    if role == "rptr"
                    else "Next producer slot; not the last valid packet."
                ),
            )

        jump_controls = []
        for role in ("rptr", "wptr"):
            label = role.upper()
            if metadata.get(role + "_view") is None:
                jump_controls.append(
                    '<span class="pointer-jump disabled" aria-disabled="true">'
                    "Jump to {}</span>".format(label)
                )
            else:
                href = "{}?pointer={}#pointer-{}".format(file_url, role, role)
                active = " active" if pointer_mode == role else ""
                jump_controls.append(
                    '<a class="pointer-jump{active}" href="{href}">'
                    "Jump to {label}</a>".format(
                        active=active,
                        href=html.escape(href, quote=True),
                        label=label,
                    )
                )
        if preview.mode != "default":
            jump_controls.append(
                '<a class="pointer-jump" href="{}">Default preview</a>'.format(
                    html.escape(file_url, quote=True)
                )
            )
        if preview.window is not None:
            window = preview.window
            context = window["context"]
            previous_href = (
                "{}?pointer={}&slot={}&context={}#window-center".format(
                    file_url,
                    pointer_mode,
                    window["previous_center"],
                    context,
                )
            )
            next_href = "{}?pointer={}&slot={}&context={}#window-center".format(
                file_url,
                pointer_mode,
                window["next_center"],
                context,
            )
            jump_controls.extend(
                [
                    '<a class="pointer-jump window-nav" href="{}">'
                    "\u2190 Previous window</a>".format(
                        html.escape(previous_href, quote=True)
                    ),
                    '<a class="pointer-jump window-nav" href="{}">'
                    "Next window \u2192</a>".format(
                        html.escape(next_href, quote=True)
                    ),
                ]
            )
            context_links = []
            for candidate in full_ring_viewer.ALLOWED_SLOT_CONTEXTS:
                href = "{}?pointer={}&slot={}&context={}#window-center".format(
                    file_url,
                    pointer_mode,
                    window["center_slot"],
                    candidate,
                )
                context_links.append(
                    '<a class="context-choice{}" href="{}">\u00b1{} slots</a>'.format(
                        " active" if candidate == context else "",
                        html.escape(href, quote=True),
                        candidate,
                    )
                )
            jump_controls.append(
                '<span class="context-choices" aria-label="Slot context">{}</span>'.format(
                    "".join(context_links)
                )
            )

        evidence = []
        seen = set()
        for item in metadata.get("evidence", []):
            source = item.get("path")
            if not source or source in seen:
                continue
            seen.add(source)
            source_parts = source.replace("\\", "/").split("/")
            if any(part in ("", ".", "..") for part in source_parts):
                continue
            href = encode_url_path(source_parts)
            line = item.get("line")
            if isinstance(line, int) and line > 0:
                href += "#L{}".format(line)
            evidence.append(
                '<a href="{}">{}</a>'.format(
                    html.escape(href, quote=True),
                    html.escape(item.get("label") or os.path.basename(source)),
                )
            )
        evidence_html = " · ".join(evidence) or "Decoded header only"

        warnings = list(metadata.get("warnings", []))
        if preview.pointer_error:
            warnings.append(
                {
                    "code": "POINTER_TARGET_UNAVAILABLE",
                    "message": preview.pointer_error,
                }
            )
        warning_html = ""
        if warnings:
            warning_html = '<div class="pointer-warnings" role="alert">{}</div>'.format(
                "".join(
                    "<p><strong>{}</strong> {}</p>".format(
                        html.escape(item.get("code", "WARNING")),
                        html.escape(item.get("message", "")),
                    )
                    for item in warnings
                )
            )

        capture_window = " / ".join(
            item
            for item in (
                metadata.get("capture_started"),
                metadata.get("capture_finished"),
            )
            if item
        )
        snapshot = metadata.get("snapshot")
        queue_id = metadata.get("queue_id")
        identity = "Snapshot {} · Queue {}".format(
            value(snapshot, "unknown"), value(queue_id, "unknown")
        )
        pending_count = metadata.get("pending_count")
        pending_summary = (
            "{} packet(s) · wraparound={}".format(
                pending_count, str(metadata.get("wraps")).lower()
            )
            if pending_count is not None
            else "Unavailable"
        )
        companion_status = metadata.get("companion_status")
        if metadata.get("self_contained"):
            companion_status = "self-contained decoded header"
        elif companion_status == "exact":
            companion_status = "exact snapshot/queue/base companion match"
        return (
            '<section class="full-ring-panel" aria-labelledby="pointer-title">'
            '<div class="pointer-panel-heading"><div>'
            '<p class="eyebrow">Queue Doctor full-ring pointers</p>'
            '<h2 id="pointer-title">{identity}</h2></div>'
            '<div class="pointer-jumps">{jumps}</div></div>'
            '<p class="interval-note"><strong>Pending is [RPTR, WPTR).</strong> '
            "RPTR is included; WPTR is excluded and names the next producer slot, "
            "not the last valid packet.</p>"
            '<div class="pointer-cards">{rptr_card}{wptr_card}</div>'
            '<dl class="ring-facts">'
            '<div><dt>Source artifact</dt><dd><code>{source}</code></dd></div>'
            '<div><dt>Ring base</dt><dd>{base}</dd></div>'
            '<div><dt>Capacity</dt><dd>{bytes} bytes · {slots} slots</dd></div>'
            '<div><dt>Pending interval</dt><dd>{pending}</dd></div>'
            '<div><dt>Pointer source</dt><dd>{pointer_source}</dd></div>'
            '<div><dt>Capture relationship</dt><dd>{atomicity}</dd></div>'
            '<div><dt>Capture window</dt><dd>{capture_window}</dd></div>'
            '<div><dt>Metadata correlation</dt><dd>{companion}</dd></div>'
            '<div><dt>Evidence</dt><dd>{evidence}</dd></div></dl>'
            "{warnings}</section>"
        ).format(
            identity=identity,
            jumps="".join(jump_controls),
            rptr_card=pointer_card("rptr"),
            wptr_card=pointer_card("wptr"),
            source=html.escape(metadata.get("source", "")),
            base=(
                "0x{:x}".format(metadata["ring_base"])
                if metadata.get("ring_base") is not None
                else "Unavailable"
            ),
            bytes=value(metadata.get("ring_size_bytes")),
            slots=value(metadata.get("slot_count")),
            pending=html.escape(pending_summary),
            pointer_source=value(metadata.get("pointer_source")),
            atomicity=value(metadata.get("atomicity")),
            capture_window=html.escape(capture_window or "Unavailable"),
            companion=html.escape(companion_status or "Unavailable"),
            evidence=evidence_html,
            warnings=warning_html,
        )

    def _render_text_viewer(
        self, parts, info, preview, full_ring=None, pointer_mode=None
    ):
        file_url = encode_url_path(parts)
        parent_url = encode_url_path(parts[:-1], directory=True)
        breadcrumbs = ['<a href="/">Bundle</a>']
        for index, part in enumerate(parts):
            label = html.escape(part)
            if index == len(parts) - 1:
                breadcrumbs.append('<span aria-current="page">{}</span>'.format(label))
            else:
                href = encode_url_path(parts[: index + 1], directory=True)
                breadcrumbs.append(
                    '<a href="{}">{}</a>'.format(html.escape(href, quote=True), label)
                )

        rendered_lines = []
        for line_number, line in zip(preview.line_numbers, preview.lines):
            roles = preview.pointer_roles.get(line_number, set())
            role_classes = "".join(" pointer-" + role for role in sorted(roles))
            is_window_center = (
                preview.window is not None
                and line_number == preview.window.get("center_line")
            )
            if is_window_center:
                role_classes += " window-center"
            anchors = "".join(
                '<span class="pointer-anchor" id="pointer-{}"></span>'.format(role)
                for role in sorted(roles)
            )
            if is_window_center:
                anchors += '<span class="pointer-anchor" id="window-center"></span>'
            badges = "".join(
                '<span class="slot-badge {}">[{}]</span>'.format(
                    role, role.upper()
                )
                for role in sorted(roles)
            )
            rendered_lines.append(
                '<div class="log-line{role_classes}" id="L{number}">{anchors}'
                '<a class="line-number" href="#L{number}" '
                'aria-label="Line {number}">{number}</a>'
                '<code>{badges}{content}</code></div>'.format(
                    number=line_number,
                    role_classes=role_classes,
                    anchors=anchors,
                    badges=badges,
                    content=highlight_log_line(line),
                )
            )

        limits = []
        if preview.byte_limited:
            limits.append("{} byte preview limit reached".format(
                human_size(MAX_PREVIEW_BYTES)
            ))
        if preview.line_limited:
            limits.append("{:,}-line preview limit reached".format(
                MAX_PREVIEW_LINES
            ))
        if preview.clipped_lines:
            limits.append(
                "{} overlong line{} clipped at {:,} characters".format(
                    preview.clipped_lines,
                    "" if preview.clipped_lines == 1 else "s",
                    MAX_PREVIEW_LINE_CHARS,
                )
            )
        if preview.mode == "slot-window":
            role = pointer_mode.upper()
            window = preview.window
            physical_range = "{} \u2192 {}".format(
                window["start_slot"], window["end_slot"]
            )
            preview_notice = (
                '<div class="preview-meta window-summary"><strong>Targeted {} '
                "slot window.</strong> {} complete physical slots rendered "
                "(range {}; wraparound={}). Logical projection: {} \u2192 {}. "
                "The selected slot is centered with complete packet blocks above "
                "and below for normal page scrolling{}.</div>"
            ).format(
                role,
                window["rendered_slot_count"],
                physical_range,
                str(window["wraps"]).lower(),
                window["logical_start"],
                window["logical_end"],
                " (bounded scan limit reached)" if preview.scan_limited else "",
            )
        elif limits:
            preview_notice = (
                '<div class="preview-notice" role="status"><strong>Preview limited.</strong> '
                '{}. Use Raw or Download for the complete file.</div>'.format(
                    html.escape("; ".join(limits))
                )
            )
        else:
            preview_notice = (
                '<div class="preview-meta">Highlighted preview \u00b7 {:,} line{} shown'
                "</div>".format(
                    len(preview.lines),
                    "" if len(preview.lines) == 1 else "s",
                )
            )

        escaped_file_url = html.escape(file_url, quote=True)
        logical_path = "/" + "/".join(parts)
        full_ring_panel = (
            self._render_full_ring_panel(
                full_ring, file_url, preview, pointer_mode
            )
            if full_ring is not None
            else ""
        )
        return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>Log Viewer \u00b7 {title}</title>
  <script>
    (() => {{
      const supported = ['light', 'dark', 'amber'];
      let theme = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
      try {{
        const saved = localStorage.getItem('gpu-bundle-theme');
        if (supported.includes(saved)) theme = saved;
      }} catch (_) {{}}
      document.documentElement.dataset.theme = theme;
    }})();
  </script>
  <style>
    :root {{
      color-scheme:light;--bg:#f3f6fb;--surface:#fff;--surface-2:#f8faff;
      --text:#1d293d;--muted:#65738a;--line:#dfe5ef;--accent:#315ecb;
      --accent-hover:#244ba9;--accent-contrast:#fff;--focus:rgba(49,94,203,.38);
      --code-bg:#fbfcff;--gutter:#f1f4f9;--shadow:0 16px 44px rgba(23,42,78,.10);
      --tok-error:#b42318;--tok-warning:#8a5700;--tok-success:#08783f;
      --tok-info:#175cd3;--tok-hex:#793fa3;--tok-number:#9b3b72;
      --tok-key:#075985;--tok-path:#705600;--tok-comment:#667085;
      --rptr:#0b7a53;--rptr-bg:#e5f8ef;--wptr:#9a4e00;--wptr-bg:#fff2dc;
    }}
    :root[data-theme="dark"] {{
      color-scheme:dark;--bg:#0f1520;--surface:#171f2d;--surface-2:#1d2737;
      --text:#edf2fb;--muted:#aab6c8;--line:#303c50;--accent:#8eafff;
      --accent-hover:#b0c6ff;--accent-contrast:#101827;--focus:rgba(142,175,255,.42);
      --code-bg:#111824;--gutter:#182231;--shadow:0 18px 50px rgba(0,0,0,.30);
      --tok-error:#ff8a80;--tok-warning:#ffd166;--tok-success:#65d89b;
      --tok-info:#82b1ff;--tok-hex:#d7a8ff;--tok-number:#ffb86c;
      --tok-key:#79c7ff;--tok-path:#f0c674;--tok-comment:#93a1b5;
      --rptr:#7be0b5;--rptr-bg:#143c32;--wptr:#ffc46b;--wptr-bg:#493318;
    }}
    :root[data-theme="amber"] {{
      color-scheme:dark;--bg:#15120c;--surface:#211b11;--surface-2:#2c2314;
      --text:#fff4d0;--muted:#d2bb7a;--line:#5d4822;--accent:#f4c95d;
      --accent-hover:#ffdc7a;--accent-contrast:#211600;--focus:rgba(244,201,93,.48);
      --code-bg:#17130d;--gutter:#241c10;--shadow:0 18px 50px rgba(5,3,0,.48);
      --tok-error:#ff9587;--tok-warning:#ffd76a;--tok-success:#a7df8b;
      --tok-info:#91cfff;--tok-hex:#e7b8ff;--tok-number:#ffbd73;
      --tok-key:#f4c95d;--tok-path:#ffe0a0;--tok-comment:#bfae82;
      --rptr:#b9df90;--rptr-bg:#26391c;--wptr:#ffcf6a;--wptr-bg:#493412;
    }}
    *{{box-sizing:border-box}}
    html{{scroll-behavior:smooth}}
    body{{margin:0;min-height:100vh;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
    .shell{{width:min(1500px,calc(100% - 28px));min-height:100vh;margin:auto;padding:30px 0 30px;display:flex;flex-direction:column}}
    header{{display:flex;flex-wrap:wrap;align-items:flex-start;justify-content:space-between;gap:18px;margin-bottom:20px}}
    .eyebrow{{margin:0 0 4px;color:var(--accent);font-size:12px;font-weight:750;letter-spacing:.12em;text-transform:uppercase}}
    h1{{margin:0;font-size:clamp(23px,3vw,34px);letter-spacing:-.03em;overflow-wrap:anywhere}}
    .subtitle{{margin:6px 0 0;color:var(--muted)}}
    .theme-control{{display:flex;align-items:center;gap:9px;border:1px solid var(--line);border-radius:10px;background:var(--surface);padding:6px 8px 6px 11px;color:var(--muted);font-size:13px;font-weight:700}}
    .theme-control select{{min-width:104px;border:1px solid var(--line);border-radius:7px;color:var(--text);background:var(--surface-2);padding:6px 28px 6px 9px;font:inherit;font-weight:700;cursor:pointer}}
    .navigation{{display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin-bottom:12px}}
    .crumbs{{display:flex;flex:1;flex-wrap:wrap;align-items:center;gap:7px;color:var(--muted);min-width:220px}}
    .crumbs a{{color:var(--accent);text-decoration:none;font-weight:650}}
    .crumbs>*+*::before{{content:"/";color:var(--muted);margin-right:7px}}
    .back,.raw,.download{{display:inline-block;border:1px solid var(--line);border-radius:9px;color:var(--text);background:var(--surface);padding:8px 12px;font-weight:650;text-decoration:none;transition:.15s}}
    .back:hover,.raw:hover{{border-color:var(--accent);color:var(--accent);transform:translateY(-1px)}}
    .download{{color:var(--accent-contrast);background:var(--accent);border-color:var(--accent)}}
    .download:hover{{color:var(--accent-contrast);background:var(--accent-hover);border-color:var(--accent-hover);transform:translateY(-1px)}}
    a:focus-visible,select:focus-visible{{outline:3px solid var(--focus);outline-offset:2px}}
    .viewer-tools{{display:flex;flex-wrap:wrap;gap:8px;margin-left:auto}}
    .file-meta{{display:flex;flex-wrap:wrap;gap:8px 18px;margin:0 0 13px;color:var(--muted);font-size:13px}}
    .file-meta code{{color:var(--text);overflow-wrap:anywhere}}
    .full-ring-panel{{margin:0 0 14px;border:1px solid var(--line);border-radius:14px;background:var(--surface);padding:16px;box-shadow:var(--shadow)}}
    .pointer-panel-heading{{display:flex;flex-wrap:wrap;align-items:flex-start;justify-content:space-between;gap:12px}}
    .pointer-panel-heading h2{{margin:0;font-size:20px}}
    .pointer-jumps{{position:sticky;top:8px;z-index:5;display:flex;flex-wrap:wrap;gap:7px;padding:4px;border:1px solid var(--line);border-radius:11px;background:var(--surface)}}
    .pointer-jump{{display:inline-block;border:1px solid var(--line);border-radius:8px;padding:7px 10px;color:var(--accent);background:var(--surface-2);font-weight:750;text-decoration:none}}
    .pointer-jump:hover,.pointer-jump.active{{border-color:var(--accent);background:var(--accent);color:var(--accent-contrast)}}
    .pointer-jump.disabled{{color:var(--muted);cursor:not-allowed;opacity:.65}}
    .context-choices{{display:flex;align-items:center;gap:3px;border-left:1px solid var(--line);padding-left:5px}}
    .context-choice{{border-radius:6px;padding:5px 7px;color:var(--muted);font-size:12px;font-weight:750;text-decoration:none}}
    .context-choice:hover,.context-choice.active{{color:var(--accent-contrast);background:var(--accent)}}
    .interval-note{{margin:13px 0;border-left:4px solid var(--accent);border-radius:7px;background:var(--surface-2);padding:10px 12px}}
    .pointer-cards{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}
    .pointer-card{{border:1px solid var(--line);border-radius:11px;padding:11px 12px;background:var(--surface-2)}}
    .pointer-card.rptr{{border-left:5px solid var(--rptr)}}.pointer-card.wptr{{border-left:5px solid var(--wptr)}}
    .pointer-card h3{{margin:0 0 8px;color:var(--text);font-size:15px}}
    .pointer-card dl,.ring-facts{{margin:0}}.pointer-card dl{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px}}
    .pointer-card dl div,.ring-facts div{{min-width:0}}dt{{color:var(--muted);font-size:12px;font-weight:750;text-transform:uppercase;letter-spacing:.04em}}
    dd{{margin:2px 0 0;overflow-wrap:anywhere}}.pointer-meaning{{margin:9px 0 0;color:var(--muted);font-size:13px}}
    .ring-facts{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:14px}}
    .ring-facts code{{color:var(--text)}}.ring-facts a{{color:var(--accent)}}
    .pointer-warnings{{margin-top:12px;border:1px solid var(--tok-warning);border-radius:9px;padding:8px 11px;color:var(--text);background:var(--surface-2)}}
    .pointer-warnings p{{margin:2px 0}}
    .preview-notice,.preview-meta{{margin:0 0 12px;border:1px solid var(--line);border-radius:10px;background:var(--surface-2);padding:10px 13px;color:var(--muted)}}
    .preview-notice{{border-left:4px solid var(--accent);color:var(--text)}}
    .log-frame{{overflow:hidden;border:1px solid var(--line);border-radius:13px;background:var(--code-bg);box-shadow:var(--shadow)}}
    .log-code{{overflow:auto;max-width:100%;font:13px/1.6 ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace;tab-size:4}}
    .log-line{{position:relative;display:grid;grid-template-columns:5.2rem max-content;min-width:100%;scroll-margin-top:9rem;border-bottom:1px solid color-mix(in srgb,var(--line) 55%,transparent)}}
    .log-line:last-child{{border-bottom:0}}
    .log-line:target,.log-line:hover{{background:var(--surface-2)}}
    .log-line.pointer-rptr{{box-shadow:inset 5px 0 var(--rptr);background:var(--rptr-bg)}}
    .log-line.pointer-wptr{{box-shadow:inset 5px 0 var(--wptr);background:var(--wptr-bg)}}
    .log-line.pointer-rptr.pointer-wptr{{box-shadow:inset 5px 0 var(--rptr),inset 10px 0 var(--wptr)}}
    .pointer-anchor{{position:absolute;top:0;left:0;width:1px;height:1px;scroll-margin-top:9rem}}
    .slot-badge{{display:inline-block;margin-right:8px;border:1px solid currentColor;border-radius:999px;padding:0 6px;font-weight:900}}
    .slot-badge.rptr{{color:var(--rptr);background:var(--rptr-bg)}}.slot-badge.wptr{{color:var(--wptr);background:var(--wptr-bg)}}
    .line-number{{position:sticky;left:0;z-index:1;padding:1px 13px 1px 8px;background:var(--gutter);color:var(--muted);border-right:1px solid var(--line);text-align:right;text-decoration:none;user-select:none;font-variant-numeric:tabular-nums}}
    .log-line code{{display:block;padding:1px 16px;white-space:pre;color:var(--text)}}
    .tok-timestamp{{color:var(--muted)}}.tok-error{{color:var(--tok-error);font-weight:800}}
    .tok-warning{{color:var(--tok-warning);font-weight:750}}.tok-success{{color:var(--tok-success);font-weight:750}}
    .tok-info{{color:var(--tok-info);font-weight:700}}.tok-hex{{color:var(--tok-hex)}}
    .tok-number{{color:var(--tok-number)}}.tok-key{{color:var(--tok-key);font-weight:700}}
    .tok-path{{color:var(--tok-path);text-decoration:underline;text-decoration-style:dotted}}
    .tok-comment{{color:var(--tok-comment);font-style:italic}}
    .release-footer{{margin-top:24px;border-top:1px solid var(--line);padding:14px 4px 0;color:var(--muted);font-size:12px;text-align:center}}
    @media(max-width:760px){{
      .shell{{width:min(100% - 14px,1500px);padding-top:18px}}
      header{{align-items:center}}.theme-control{{width:100%;justify-content:space-between}}
      .theme-control select{{flex:1}}.viewer-tools{{width:100%;margin-left:0}}
      .raw,.download{{flex:1;text-align:center}}.log-code{{font-size:12px}}
      .log-line{{grid-template-columns:3.6rem max-content}}
      .line-number{{padding-right:9px}}.log-line code{{padding-right:12px}}
      .pointer-cards,.ring-facts{{grid-template-columns:1fr}}.pointer-jumps{{width:100%}}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <div><p class="eyebrow">Highlighted log viewer</p><h1>{title}</h1>
        <p class="subtitle">Safe, bounded preview with diagnostic token highlighting.</p></div>
      <label class="theme-control" for="theme-select"><span>Theme</span>
        <select id="theme-select" aria-label="Choose visual theme">
          <option value="light">Light</option><option value="dark">Dark</option>
          <option value="amber">\u6697\u9ec4</option>
        </select>
      </label>
    </header>
    <nav class="navigation" aria-label="File navigation">
      <a class="back" href="{parent_url}">\u2190 Back</a>
      <div class="crumbs">{breadcrumbs}</div>
      <div class="viewer-tools">
        <a class="raw" href="{file_url}?raw=1">Raw</a>
        <a class="download" href="{file_url}?download=1">Download</a>
      </div>
    </nav>
    <p class="file-meta"><span>Path: <code>{logical_path}</code></span>
      <span>Size: {size}</span><span>Modified: {modified}</span></p>
    {full_ring_panel}
    {preview_notice}
    <section class="log-frame" aria-label="Highlighted file contents">
      <div class="log-code">{rendered_lines}</div>
    </section>
    {release_footer}
  </main>
  <script>
    const themeSelect = document.getElementById('theme-select');
    themeSelect.value = ['light', 'dark', 'amber'].includes(document.documentElement.dataset.theme)
      ? document.documentElement.dataset.theme : 'light';
    themeSelect.addEventListener('change', () => {{
      document.documentElement.dataset.theme = themeSelect.value;
      try {{ localStorage.setItem('gpu-bundle-theme', themeSelect.value); }} catch (_) {{}}
    }});
  </script>
</body>
</html>
""".format(
            title=html.escape(parts[-1]),
            parent_url=html.escape(parent_url, quote=True),
            breadcrumbs="".join(breadcrumbs),
            file_url=escaped_file_url,
            logical_path=html.escape(logical_path),
            size=html.escape(human_size(info.st_size)),
            modified=html.escape(format_modified(info.st_mtime)),
            full_ring_panel=full_ring_panel,
            preview_notice=preview_notice,
            rendered_lines="".join(rendered_lines),
            release_footer=release_footer(),
        )

    def _render_directory(self, parts, entries):
        breadcrumbs = ['<a href="/">Bundle</a>']
        for index, part in enumerate(parts):
            label = html.escape(part)
            if index == len(parts) - 1:
                breadcrumbs.append('<span aria-current="page">{}</span>'.format(label))
            else:
                href = encode_url_path(parts[: index + 1], directory=True)
                breadcrumbs.append(
                    '<a href="{}">{}</a>'.format(html.escape(href, quote=True), label)
                )

        if parts:
            parent_url = encode_url_path(parts[:-1], directory=True)
            back = '<a class="back" href="{}">\u2190 Back</a>'.format(
                html.escape(parent_url, quote=True)
            )
        else:
            back = '<span class="back disabled" aria-disabled="true">\u2190 Back</span>'

        rows = []
        for item in entries:
            href = encode_url_path(
                parts + (item.name,), directory=item.is_directory
            )
            escaped_href = html.escape(href, quote=True)
            icon = "\U0001f4c1" if item.is_directory else "\U0001f4c4"
            name_markup = (
                '<a class="name" href="{href}"><span class="icon" '
                'aria-hidden="true">{icon}</span><span>{name}</span></a>'
            ).format(
                href=escaped_href,
                icon=icon,
                name=html.escape(item.name),
            )
            if item.is_directory:
                action = '<a class="open" href="{}">Open</a>'.format(escaped_href)
            else:
                action = '<a class="download" href="{}">Download</a>'.format(
                    html.escape(href + "?download=1", quote=True)
                )
            rows.append(
                "<tr><td data-label=\"Name\">{name}</td>"
                "<td data-label=\"Type\">{kind}</td>"
                "<td data-label=\"Modified\"><time>{modified}</time></td>"
                "<td data-label=\"Size\" class=\"size\">{size}</td>"
                "<td class=\"action\">{action}</td></tr>".format(
                    name=name_markup,
                    kind='<span class="badge">{}</span>'.format(
                        "Directory" if item.is_directory else "File"
                    ),
                    modified=html.escape(format_modified(item.modified)),
                    size=html.escape(human_size(item.size)),
                    action=action,
                )
            )
        if not rows:
            rows.append(
                '<tr><td class="empty" colspan="5">This directory is empty.</td></tr>'
            )

        dashboard_action = ""
        if not parts and dashboard.is_gpu_debug_bundle(self.root_directory):
            dashboard_action = (
                '<a class="dashboard-action" href="/__dashboard__/">'
                "Analyze GPU queues</a>"
            )

        logical_path = "/" + "/".join(parts)
        return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>GPU Debug Bundle \u00b7 {title}</title>
  <script>
    (() => {{
      const supported = ['light', 'dark', 'amber'];
      let theme = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
      try {{
        const saved = localStorage.getItem('gpu-bundle-theme');
        if (supported.includes(saved)) theme = saved;
      }} catch (_) {{}}
      document.documentElement.dataset.theme = theme;
    }})();
  </script>
  <style>
    :root {{
      color-scheme: light;
      --bg:#f3f6fb;--surface:#fff;--surface-2:#f8faff;--text:#1d293d;
      --muted:#65738a;--line:#dfe5ef;--accent:#315ecb;--accent-hover:#244ba9;
      --accent-contrast:#fff;--badge-bg:#edf2ff;--focus:rgba(49,94,203,.38);
      --shadow:0 16px 44px rgba(23,42,78,.10);
    }}
    :root[data-theme="dark"] {{
      color-scheme:dark;--bg:#0f1520;--surface:#171f2d;--surface-2:#1d2737;
      --text:#edf2fb;--muted:#aab6c8;--line:#303c50;--accent:#8eafff;
      --accent-hover:#b0c6ff;--accent-contrast:#101827;--badge-bg:#24314a;
      --focus:rgba(142,175,255,.42);--shadow:0 18px 50px rgba(0,0,0,.30);
    }}
    :root[data-theme="amber"] {{
      color-scheme:dark;--bg:#15120c;--surface:#211b11;--surface-2:#2c2314;
      --text:#fff4d0;--muted:#d2bb7a;--line:#5d4822;--accent:#f4c95d;
      --accent-hover:#ffdc7a;--accent-contrast:#211600;--badge-bg:#3b2e13;
      --focus:rgba(244,201,93,.48);--shadow:0 18px 50px rgba(5,3,0,.48);
    }}
    *{{box-sizing:border-box}}
    body{{margin:0;min-height:100vh;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
    .shell{{width:min(1180px,calc(100% - 32px));min-height:100vh;margin:auto;padding:42px 0 30px;display:flex;flex-direction:column}}
    header{{display:flex;flex-wrap:wrap;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:24px}}
    .eyebrow{{margin:0 0 5px;color:var(--accent);font-size:12px;font-weight:750;letter-spacing:.12em;text-transform:uppercase}}
    h1{{margin:0;font-size:clamp(25px,4vw,38px);letter-spacing:-.035em}}
    .subtitle{{margin:7px 0 0;color:var(--muted)}}
    .header-actions{{display:flex;align-items:center;flex-wrap:wrap;gap:9px}}
    .theme-control{{display:flex;align-items:center;gap:9px;border:1px solid var(--line);border-radius:10px;background:var(--surface);padding:6px 8px 6px 11px;color:var(--muted);font-size:13px;font-weight:700}}
    .theme-control select{{min-width:104px;border:1px solid var(--line);border-radius:7px;color:var(--text);background:var(--surface-2);padding:6px 28px 6px 9px;font:inherit;font-weight:700;cursor:pointer}}
    .back,.download,.open{{border:1px solid var(--line);border-radius:9px;color:var(--text);background:var(--surface);padding:8px 12px;font:inherit;font-weight:650;text-decoration:none;cursor:pointer;transition:.15s}}
    .dashboard-action{{display:inline-block;border:1px solid var(--accent);border-radius:9px;background:var(--accent);color:var(--accent-contrast);padding:9px 13px;font-weight:750;text-decoration:none}}
    .dashboard-action:hover{{background:var(--accent-hover);border-color:var(--accent-hover);transform:translateY(-1px)}}
    .back:not(.disabled):hover,.open:hover{{border-color:var(--accent);color:var(--accent);transform:translateY(-1px)}}
    a:focus-visible,select:focus-visible{{outline:3px solid var(--focus);outline-offset:2px}}
    .disabled{{opacity:.45;cursor:default}}
    .toolbar{{display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin-bottom:14px}}
    .crumbs{{display:flex;flex:1;flex-wrap:wrap;align-items:center;gap:7px;color:var(--muted);min-width:220px}}
    .crumbs a{{color:var(--accent);text-decoration:none;font-weight:650}}
    .crumbs>*+*::before{{content:"/";color:var(--muted);margin-right:7px}}
    .path{{margin:0 0 14px;color:var(--muted);font:13px/1.4 ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere}}
    .card{{overflow:hidden;border:1px solid var(--line);border-radius:14px;background:var(--surface);box-shadow:var(--shadow)}}
    table{{width:100%;border-collapse:collapse}}
    th{{padding:12px 16px;background:var(--surface-2);color:var(--muted);font-size:12px;letter-spacing:.06em;text-align:left;text-transform:uppercase}}
    td{{padding:13px 16px;border-top:1px solid var(--line);vertical-align:middle}}
    tbody tr:hover{{background:var(--surface-2)}}
    .name{{display:flex;align-items:center;gap:10px;color:var(--text);font-weight:650;text-decoration:none;overflow-wrap:anywhere}}
    .name:hover{{color:var(--accent)}}.icon{{font-size:19px}}
    .badge{{display:inline-block;border:1px solid var(--line);border-radius:999px;background:var(--badge-bg);color:var(--text);padding:2px 8px;font-size:12px;font-weight:700}}
    .size{{white-space:nowrap;font-variant-numeric:tabular-nums}}
    time{{color:var(--muted);white-space:nowrap;font-variant-numeric:tabular-nums}}
    .action{{text-align:right;width:1%}}
    .download{{display:inline-block;color:var(--accent-contrast);background:var(--accent);border-color:var(--accent)}}
    .download:hover{{color:var(--accent-contrast);background:var(--accent-hover);border-color:var(--accent-hover);transform:translateY(-1px)}}
    .open{{display:inline-block}}.empty{{padding:48px 20px;color:var(--muted);text-align:center}}
    .directory-note{{margin:18px 0;color:var(--muted);font-size:13px;text-align:center}}
    .release-footer{{margin-top:auto;border-top:1px solid var(--line);padding:14px 4px 0;color:var(--muted);font-size:12px;text-align:center}}
    @media(max-width:760px){{
      .shell{{width:min(100% - 20px,1180px);padding-top:24px}}header{{align-items:center}}
      .card{{border:0;background:transparent;box-shadow:none;overflow:visible}}
      table,tbody,tr,td{{display:block;width:100%}}thead{{display:none}}
      tr{{margin-bottom:10px;padding:13px;border:1px solid var(--line);border-radius:12px;background:var(--surface);box-shadow:0 7px 22px rgba(23,42,78,.07)}}
      td{{display:grid;grid-template-columns:92px 1fr;gap:8px;padding:4px 0;border:0}}
      td::before{{content:attr(data-label);color:var(--muted);font-size:12px;font-weight:700;text-transform:uppercase}}
      td:first-child{{display:block;padding-bottom:10px}}td:first-child::before,.action::before{{display:none}}
      .action{{display:block;padding-top:12px;text-align:left}}.download,.open{{width:100%;text-align:center}}time{{white-space:normal}}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <div><p class="eyebrow">Diagnostics</p><h1>GPU Debug Bundle</h1>
        <p class="subtitle">Browse and download collected GPU diagnostics.</p></div>
      <div class="header-actions">{dashboard_action}<label class="theme-control" for="theme-select">
        <span>Theme</span>
        <select id="theme-select" aria-label="Choose visual theme">
          <option value="light">Light</option>
          <option value="dark">Dark</option>
          <option value="amber">\u6697\u9ec4</option>
        </select>
      </label></div>
    </header>
    <nav class="toolbar" aria-label="Directory navigation">{back}<div class="crumbs">{breadcrumbs}</div></nav>
    <p class="path">Current path: {logical_path}</p>
    <section class="card" aria-label="Directory contents">
      <table><thead><tr><th>Filename</th><th>Type</th><th>Modified</th><th>Size</th><th></th></tr></thead>
        <tbody>{rows}</tbody></table>
    </section>
    <p class="directory-note">Text and log files open in the highlighted viewer. Use Download to save an attachment.</p>
    {release_footer}
  </main>
  <script>
    const themeSelect = document.getElementById('theme-select');
    themeSelect.value = ['light', 'dark', 'amber'].includes(document.documentElement.dataset.theme)
      ? document.documentElement.dataset.theme : 'light';
    themeSelect.addEventListener('change', () => {{
      const next = themeSelect.value;
      document.documentElement.dataset.theme = next;
      try {{ localStorage.setItem('gpu-bundle-theme', next); }} catch (_) {{}}
    }});
  </script>
</body>
</html>
""".format(
            title=html.escape(parts[-1] if parts else "Bundle"),
            back=back,
            breadcrumbs="".join(breadcrumbs),
            logical_path=html.escape(logical_path),
            rows="".join(rows),
            dashboard_action=dashboard_action,
            release_footer=release_footer(),
        )

    def log_message(self, message_format, *args):
        sys.stderr.write(
            "{} - - [{}] {}\n".format(
                self.client_address[0],
                self.log_date_time_string(),
                message_format % args,
            )
        )


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Python 3.6-compatible threaded HTTP server."""

    daemon_threads = True
    allow_reuse_address = True


def make_handler(root_directory):
    """Create a request handler fixed to a validated bundle root."""
    requested_root = os.path.abspath(root_directory)
    root = os.path.realpath(requested_root)
    if not os.path.isdir(root):
        raise ValueError("bundle root is not a directory")
    if os.path.islink(requested_root):
        raise ValueError("bundle root must not be a symbolic link")

    class RootedGPUBundleRequestHandler(GPUBundleRequestHandler):
        pass

    RootedGPUBundleRequestHandler.root_directory = root
    return RootedGPUBundleRequestHandler


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(
        description="Serve one GPU debug bundle with a safe web directory browser."
    )
    parser.add_argument("root", help="GPU debug bundle root directory")
    parser.add_argument("--bind", default="0.0.0.0", help="bind address")
    parser.add_argument("--port", type=int, default=30000, help="TCP port")
    return parser.parse_args(argv)


def main(argv=None):
    arguments = parse_arguments(argv)
    try:
        handler = make_handler(arguments.root)
    except ValueError as error:
        print("error: {}".format(error), file=sys.stderr)
        return 2
    try:
        server = ThreadingHTTPServer((arguments.bind, arguments.port), handler)
    except OSError as error:
        print("error: cannot start server: {}".format(error), file=sys.stderr)
        return 1

    print(
        "GPU debug bundle browser listening on http://{}:{}/".format(
            arguments.bind, server.server_address[1]
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.", file=sys.stderr)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
