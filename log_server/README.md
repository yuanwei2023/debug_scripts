# GPU debug bundle log server

Serve one collected log directory with a dependency-free, responsive directory
browser:

```bash
./start_log_server.sh /path/to/gpu_debug_bundle
```

The launcher defaults to `0.0.0.0:30000`. Options may appear before or after
the directory:

```bash
./start_log_server.sh "/path/with spaces/logs" --port 30000 --bind 0.0.0.0
```

The server runs in the foreground and exits cleanly on `Ctrl-C`. For a
temporary background session, redirect its output explicitly:

```bash
nohup ./start_log_server.sh /path/to/logs >log-server.out 2>&1 &
```

The **Theme** selector offers Light, Dark, and **暗黄** (dark amber/gold)
themes. The browser remembers an explicit selection in local storage; the
system light/dark preference is used only when no saved selection exists.

Run the focused unit tests with:

```bash
python3 test_server.py
```

Directory URLs are constrained to the configured root. Traversal, malformed
UTF-8 paths, symlinks, and non-regular files are rejected or omitted. Filename
links open files inline; the separate **Download** action sends an attachment
header.

## Highlighted text viewer

Clicking a `.txt` or `.log` file opens the built-in, offline log viewer with
line numbers and safe highlighting for timestamps, severity/status words,
addresses, numbers, field names, paths, and unambiguous comment lines. All
source text is HTML-escaped before token markup. Invalid UTF-8 bytes are shown
with the Unicode replacement character.

The viewer reads at most 256 KiB and renders at most 2,000 lines. Individual
lines are clipped at 16,384 characters, and highlighting is capped per line.
A visible notice identifies any limit that was reached. **Raw** streams the
original file inline and **Download** streams it with an attachment header, so
the complete file remains available. Other file extensions retain their
original browser/MIME handling.

## Release footer

Every generated HTML page shows static release metadata such as
`v1.2.0 · Released 2026-08-21 13:38 UTC+8`. This is the server release time,
not the process start time. For a new release, update both
`SERVER_SEMANTIC_VERSION` and `SERVER_RELEASE_TIMESTAMP` near the top of
`server.py`; all directory, viewer, and error footers read those single-source
constants.
