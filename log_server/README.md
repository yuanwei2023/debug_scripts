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

## GPU queue dashboard

When the served root contains a recognizable GPU debug bundle, the root page
shows **Analyze GPU queues** and enables the stable `/__dashboard__/` route.
The dependency-free dashboard correlates CPC queues with KFD HQDs and Queue
Doctor records only through exact MQD addresses inside independently known
PCI/XCC context. Filename `gpuN`, UMR instance, KFD node/gpu_id, PCI BDF,
render node, and AMD-SMI index remain visibly distinct namespaces.

The overview and per-GPU/XCC tables include active/non-empty/empty counts,
MQD/PQ addresses, queue capacity, raw RPTR/WPTR and physical AQL slots, VMID,
sampled queue/doorbell bits, pending packet summaries, source links, and
explicit unmatched or ambiguous joins. Search and filters operate entirely in
the browser without a network dependency. **Refresh / re-analyze** bypasses
the in-memory cache; normal requests cache by relevant source mtimes and sizes.

Queue Doctor's repeated same-context snapshots may label a row a **stall
candidate**. A single CPC/KFD snapshot is never called a confirmed hang.
Sources were captured non-atomically and at different times, so every page
retains that caveat. The large Queue Doctor `report.json` is deliberately not
loaded; bounded summaries, queue listings, and pending packet text provide the
dashboard fields without embedding huge raw logs.

Run the focused unit tests with:

```bash
python3 -m unittest -v test_server.py test_dashboard.py test_full_ring_viewer.py
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

Queue Doctor decoded full-ring artifacts are recognized from their header and
physical-slot structure, not only their filename. Their viewer adds validated
ring geometry and RPTR/WPTR cards with logical indices, physical slots, byte
offsets, absolute ring addresses, pointer provenance, capture atomicity, and
source evidence. Pending always means the half-open interval `[RPTR, WPTR)`;
WPTR is the next producer slot and is not the last valid packet.

The exact RPTR and WPTR slot lines receive visible badges and stable anchors.
**Jump to RPTR** and **Jump to WPTR** open bounded, targeted slot
windows centered on the selected slot. The default includes 24 complete
physical packet blocks before and after the target, preserving every
multi-line body and signal record and allowing normal continuous page
scrolling in both directions. Circular windows wrap safely across slot zero
without duplicates. **Previous window** / **Next window** and ±16/24/32
context controls provide bounded fallback navigation without rendering an
entire 16,384-slot ring. Equal pointers show both badges on one slot;
unavailable pointers disable their controls. Older decoded artifacts may
recover missing pointers only from an exact
snapshot/queue/ring-base/capacity match in bounded Queue Doctor state or JSON
metadata. Conflicting companion records are shown as ambiguous and rejected.

## Release footer

Every generated HTML page shows static release metadata such as
`v2.1.1 · Released 2026-08-21 16:49 UTC+8`. This is the server release time,
not the process start time. For a new release, update both
`SERVER_SEMANTIC_VERSION` and `SERVER_RELEASE_TIMESTAMP` near the top of
`server.py`; directory, viewer, dashboard, and error footers read those
single-source constants.
