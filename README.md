# ROCm Forensics Toolkit

[简体中文](README_CN.md) | **English**

> A diagnostic collection toolkit for AMD GPU queues, KFD, SDMA, CPC, waves,
> and ROCclr runtime failures.

This project provides scripts for investigating AMD GPU hangs, queue stalls,
signal waits, SDMA timeouts, and firmware issues. The tools correlate
processes, KFD queues, GPU topology nodes, and PCI BDFs, then save the
diagnostic data as structured, archive-ready debug bundles for offline
analysis.

## Key capabilities

- Automatically discover the GPUs and KFD queues used by a target process
- Capture multiple queue RPTR/WPTR, MQD, and pending-packet snapshots
- Collect KFD runlists, HQDs, MQDs, fences, trace events, and kernel logs
- Read CPC, wave, and SDMA state from every GPU/XCC
- Collect ROCclr/AMD runtime logs from Docker containers
- Dump the full retained user-queue ring for a process
- Read process GPU virtual memory and queue-visible HSA signal values
- Selectively enable debug logs by DRM category and kernel source file
- Generate timestamped directories, summaries, and compressed archives

## Requirements

- Linux with the AMDGPU/KFD driver
- Python 3.8 or later
- Bash 4.4 or later
- [UMR](https://gitlab.freedesktop.org/tomstdenis/umr)
- `amd-smi`
- `sudo` access for debugfs, KFD, and selected hardware state
- Optional: Docker, CMake, RPM, and CPack tools

Run these tools on a dedicated debugging node when possible. The target GPU
process must still be running to access its KFD queue metadata and GPUVM ring
contents.

## Quick start

### 1. Build UMR

Run the build as a regular user. The script invokes `sudo` only when installing
dependencies:

```bash
./00_pull_build_umr.sh
```

The default source is the upstream UMR `main` branch. To generate an RPM:

```bash
./00_pull_build_umr.sh --package
```

### 2. Collect a complete debug bundle

Specify the target process PID (required):

```bash
./06_collect_all_gpu_debug.sh --pid 4008481
```

Full physical AQL rings are captured by default. To opt out:

```bash
./06_collect_all_gpu_debug.sh --pid 4008481 --no-full-ring
```

Successful raw `.bin` captures are also retained by default. To keep only the
decoded report after a successful decode:

```bash
./06_collect_all_gpu_debug.sh --pid 4008481 --discard-full-ring-raw
```

The default output directory is:

```text
/tmp/debug-tools-<UID>/gpu_debug_bundle_<HOST>_<TIMESTAMP>/
```

Override the default output root with an environment variable:

```bash
DEBUG_TOOLS_TMP_DIR=/var/tmp/gpu-debug \
  ./06_collect_all_gpu_debug.sh --pid 4008481
```

## Script reference

### `00_pull_build_umr.sh`

Clone or update UMR and build it with automatic LLVM detection, a no-LLVM
fallback, optional dependency installation, and RPM packaging.

```bash
./00_pull_build_umr.sh --install-deps
./00_pull_build_umr.sh --branch main --package
```

### `02_umr_queue_doctor.py`

The primary queue diagnostic tool. It captures multiple queue snapshots and
analyzes RPTR/WPTR progress, head packets, pending packets, MQD state, and
cross-source conflicts.

Each selected AQL queue is dumped in two independent paths by default: the
current/pending view is written under `pending/`, and an exact
`hqd_base_addr`/`rb_buf_size` GPUVM read is written under `full-ring/` as a
raw `.bin` plus a `.decoded.txt` report. UMR reports `rb_buf_size` in
32-bit words, so Queue Doctor multiplies it by four to obtain the byte capacity.
The report covers every physical 64-byte slot, including every physical slot
mapped from the logical pending interval `[RPTR, WPTR)` across wraparound.
Pending slot headers carry a `[PENDING logical=N]` annotation, and the report
records the expected/decoded/missing pending-slot sets so omission cannot be
silent. The UMR-style layout starts with `Dumping ...` and `Decoding IB at ...`
lines followed by
`[vmid@base + offset] [raw header] Opcode ... [HSA_*]` packet lines and
indented `|---> field=value` body/signal lines at their packet-relative byte
offsets. The decoded text does not duplicate each slot's complete 64-byte
payload; the exact ring bytes remain in the retained raw `.bin` artifact.
Queue/HQD/doorbell, RPTR/WPTR, and dispatch metadata remains in the report
header. It is not JSONL. An INVALID header is always displayed as authoritative
type `HSA_INVALID`; an internal residual-body interpretation may still decode
useful subordinate fields, but the decoded text omits the residual-body type
annotation. These fields never change the header type or claim consumption,
ownership, or current state. Nonzero barrier dependency handles use bounded,
cached reads: readable signals show `value` and `status=PENDING|COMPLETED`;
unreadable signals show only the handle. A zero barrier dependency handle
means no dependency/already satisfied, so its full-ring text shows only
`dep_signal[N]=0x0`. Readable nonzero completion/watch signals likewise show
their value and `PENDING`/`COMPLETED` status; null or unreadable handles show
only the handle, without unavailable-status or reason lines. Structured slot
records remain in `report.json`. Both artifacts are retained by default and
listed in `summary.txt` and `report.json`. Use
`--discard-full-ring-raw` to delete `.bin` only after successful decode.
Failed or incomplete decodes always retain available raw data and record the
reason in the decoded report, even when discard was requested.
`--keep-full-ring-raw` remains accepted as a backward-compatible, redundant
spelling of the default policy.
It records the VM command, exact byte count, SHA-256, exit/timeout/error,
bounded signal reads, queue-level doorbell/dispatch metadata, and the exact
PCI, KFD GPU ID, UMR-local queue selector, VM partition/XCC, GFX-hub, and VMID
context used for the process-GPUVA read. If every pending header is
authoritatively `HSA_INVALID`, Queue Doctor emits a warning to compare the
pending dump and verify that context; it does not relabel the packets. Use
`--no-full-ring` only when capture cost must be avoided. Full-ring failure is
isolated from pending collection.

```bash
python3 02_umr_queue_doctor.py --pid 4008481
python3 02_umr_queue_doctor.py --pid 4008481 --gpu-id 0 1
python3 02_umr_queue_doctor.py \
  --pid 4008481 --queue 28 --samples 3 --interval 10
python3 02_umr_queue_doctor.py \
  --pid 4008481 --discard-full-ring-raw
```

Queue Doctor keeps the authoritative PID target inventory and selected-GPU
diagnostics in persisted reports without echoing the verbose source-of-truth,
per-GPU inventory, or queue-namespace block during a normal standalone
capture. When invoked through `06_collect_all_gpu_debug.sh`, the inventory
preflight runs without a visible collector `BEGIN`/`END` section; its output
remains in `summary.txt` and `collector.txt`, while Queue Doctor also preserves
the metadata in `target-gpus.json` and `report.json`. GPUs visible to the
system but absent from the PID's KFD queue inventory remain filtered
internally and produce no per-GPU output or dump artifacts. A persisted
inventory header is:

```text
Target GPUs
===========
  AMD-SMI=4 / KFD node=6 / gpu_id=61984 / PCI=0001:0b:00.0 / renderD160 / KFD queues=2 / KFD queue IDs=[41, 77]

Queue Doctor selected GPUs:
  DUMP AMD-SMI=4 / KFD node=6 / gpu_id=61984 / PCI=0001:0b:00.0 / renderD160
```

When `--pid` is omitted, the tool uses `amd-smi` to find
`torch_server.bin`.

### `03_dump_sdma_registers.sh`

Read status, GFX, PAGE, RLC, and XNACK registers from every GPU and SDMA
instance. This collector is read-only and never writes hardware registers.

Before any register read, the script resolves `UMR` (executable or source tree),
records the UMR path and version in the log header, and verifies that the
selected UMR is **>= 1.0.11** and supports the commands this collector uses
(`--script instances`, `-lb`, `-vmp`, `-O`, `-r`). An older or incomplete UMR
fails early with an actionable error. Build or refresh UMR with
`./00_pull_build_umr.sh` when needed, or set `UMR=/path/to/newer/umr`.

Each GPU uses **one batched UMR read** for all discovered SDMA blocks on that
GPU (block discovery via per-GPU `-lb` is still separate because SDMA block
names are topology-specific and are not cached across GPUs). Cross-GPU
collection runs concurrently for all discovered GPUs by default; use
`--jobs N` to limit concurrency or `--jobs 1` for sequential behavior. Each
UMR invocation has a configurable timeout (`--timeout` / `UMR_TIMEOUT_SEC`,
default **600** seconds), so a hung UMR cannot block the entire run. The log
records parallel job count, timeout, UMR invocation strategy/counts, and
per-GPU worker timings.

```bash
./03_dump_sdma_registers.sh
./03_dump_sdma_registers.sh --vmp 1 -o /tmp/sdma-registers.txt
./03_dump_sdma_registers.sh --jobs 4 --timeout 900
```

Offline regression tests: `./tests/run_sdma_register_dump_test.sh`

### `04_dump_kfd_snapshots.sh`

Collect a bounded number of KFD runlist, HQD, MQD, AMD-SMI, fence, trace, and
kernel-log snapshots. The script never follows `trace_pipe` or `dmesg`
indefinitely.

```bash
./04_dump_kfd_snapshots.sh --snapshots 3 --interval 20
./04_dump_kfd_snapshots.sh --dynamic-debug
```

### `15_trace_kfd_queue.sh`

Standalone KFD queue/runlist dynamic-debug helper. It enables selected
`printk` callsites in KFD source files (by file, format string, or func name)
and sets `amdgpu.debug_evictions=1` and `amdgpu.halt_if_hws_hang=1`, then
optionally follows the kernel journal. Eviction worker funcs use `+pfl`; other
rules use `+p`. This script is **not** invoked by `06_collect_all_gpu_debug.sh`.

```bash
./15_trace_kfd_queue.sh status
./15_trace_kfd_queue.sh enable
./15_trace_kfd_queue.sh capture ./kfd-queue.txt
./15_trace_kfd_queue.sh disable
```

Requirements:

- `CONFIG_DYNAMIC_DEBUG` (`/proc/dynamic_debug/control` or debugfs equivalent)
- `sudo` access
- Loaded `amdgpu` module with `debug_evictions` and `halt_if_hws_hang` sysfs
  parameters

`capture` enables debug, tees `journalctl -k -f` into LOG until Ctrl-C, and
leaves debug enabled; run `disable` explicitly when finished.

### `05_dump_all_cpc_info.sh`

Collect CPC, wave, KFD runlist, firmware, module parameter, and kernel-log
state from every GPU/XCC, then create a `.tgz` archive.

```bash
./05_dump_all_cpc_info.sh
```

This script checks and may enable `amdgpu.halt_if_hws_hang`. Use it only in a
controlled debugging environment.

The CPC collector probes UMR help once and uses the command supported by that
installation: current UMR releases use `--tool cpc --`, while releases before
the July 2026 tool-interface change use `--print-cpc`. CPC stdout is saved per
GPU/XCC; an unsupported interface fails before per-XCC collection instead of
repeating an unknown-argument warning.

### `06_collect_all_gpu_debug.sh`

The recommended entry point. It runs Queue Doctor, SDMA, KFD, CPC, and wave
collectors sequentially and produces one unified debug bundle. `--pid` is
required; the script does not auto-discover target PIDs.

```bash
./06_collect_all_gpu_debug.sh --pid 4008481
./06_collect_all_gpu_debug.sh \
  --pid 4008481 --samples 3 --interval 5 --vmp 0
```

Use `--skip-queue`, `--skip-sdma`, `--skip-kfd`, `--skip-cpc`,
`--skip-waves`, or `--skip-aql-queue` to disable individual collectors.
Successful Queue Doctor `.bin` captures are retained by default.
`--discard-full-ring-raw` forwards the successful-decode cleanup opt-out.
The legacy `--keep-full-ring-raw` flag is still forwarded but is redundant.

When enabled, the bundle also includes an AQL/SDMA gdb dump under
`aql_queue_dump/` (or `aql_queue_dump_pid<PID>/` for multiple targets).
Optional `GDB` and `LD_PRELOAD` environment variables are forwarded to script
`13` when you set them before running `06`.
Queue Doctor keeps its dump directories in the bundle layout and reports final
status/count diagnostics without printing standalone per-directory path lines.

Every invoked collector, including target inventory and AQL GDB, records
integer wall-clock execution seconds from a monotonic clock when available.
Regular visible collector steps and `collector.txt` use these completion
formats:

```text
[collector] END queue_doctor: PASS elapsed=12s
[collector] END cpc_dump: FAIL rc=17; continuing elapsed=3s
[collector] END aql_queue_dump: FAIL rc=124; continuing elapsed=120s
```

The target-inventory preflight records its `BEGIN`/`END` and elapsed time only
in `collector.txt`/`summary.txt`, not in terminal output.

The same `elapsed=<N>s` token is the fourth tab-separated field of each invoked
step in `summary.txt`; skipped steps use `-`. `total_elapsed=<N>s` and
`[collector] TOTAL elapsed=<N>s` report the overall wrapper duration, including
archive creation when enabled. Queue Doctor's nested pending/full-ring timing
remains independently scoped inside its own artifacts when present: those
operations are already included in the outer `queue_doctor` elapsed value and
must not be added to it.

### `08_dump_all_gpu_waves.sh`

Capture multiple wave snapshots from every UMR GPU/XCC.

```bash
./08_dump_all_gpu_waves.sh --samples 5 --interval 5
./08_dump_all_gpu_waves.sh --halt-waves
```

`--halt-waves` is intrusive and must only be used on a dedicated debugging
system.

### `09_collect_docker_amd_log.sh`

Discover Docker containers running the target process, copy AMD runtime logs,
and create a compressed archive.

```bash
./09_collect_docker_amd_log.sh
./09_collect_docker_amd_log.sh --container my-container
./09_collect_docker_amd_log.sh --pid 138732
```

### `10_dump_full_user_queue_rings.sh`

Discover queues from KFD sysfs by PID, map each queue `gpuid` to the correct PCI
GPU, and use UMR `use_full_user_queue` to dump the full retained ring.

```bash
./10_dump_full_user_queue_rings.sh --pid 875176
./10_dump_full_user_queue_rings.sh --pid 875176 --queue 28
./10_dump_full_user_queue_rings.sh \
  --pid 875176 --pci 0000:0a:00.0 --vm-partition 0
```

### `11_configure_drm_debug.sh`

Selectively enable or disable `drm.debug` categories and dynamic-debug
callsites in specified kernel source files. Only explicitly selected categories
and files are changed.

```bash
./11_configure_drm_debug.sh enable --drm driver,kms \
  --file amdgpu_job.c --file kfd_chardev.c
./11_configure_drm_debug.sh disable --drm kms --file amdgpu_job.c
./11_configure_drm_debug.sh show --file kfd_chardev.c
```

Quote file globs, for example
`--file 'drivers/gpu/drm/amd/amdgpu/*.c'`. Settings remain active until they are
disabled, changed again, or the system reboots. Add `--dry-run` to inspect all
writes first.

### `12_read_process_gpu_memory.py`

Bind UMR to a live KFD process and read a GPU virtual address, a half-open
address range, one or more HSA signal handles, or signal handles discovered
from the process queue packets.

```bash
./12_read_process_gpu_memory.py --pid 1234 --address 0x7f0000001000
./12_read_process_gpu_memory.py --pid 1234 \
  --range 0x7f0000001000 0x7f0000001100
./12_read_process_gpu_memory.py --pid 1234 \
  --signal 0x7f0000123400
./12_read_process_gpu_memory.py --pid 1234 --all-signals
./12_read_process_gpu_memory.py --pid 1234 --all-signals --full-ring
```

`--all-signals` reports nonzero `dep_signal` and `completion_signal` handles,
plus recognizable HSA signal addresses used by SDMA poll/atomic packets.
These are all signals visible in the captured queues, not every signal
allocated internally by ROCr. `--full-ring` also scans retained historical
packets, so some discovered handles can already be destroyed and appear as
`invalid/stale`.

### `13_dump_aql_queues_gdb.sh`

Before attaching GDB, capture a bounded, noninteractive snapshot from the exact
executable `/opt/rocm/bin/amd-smi`. Then attach GDB to a live process and decode
HSA AQL queues plus SDMA blit rings by sourcing `aql_packet_decode_gdb.py`. The
script does not start workloads or change ROCm runtime settings inside the
target process.

Requirements: the selected GDB must be executable and support Python scripting.
Script `13` does not check or require a minimum or specific GDB version. Default
is `/usr/bin/gdb`; override with `GDB` or `--gdb`.

```bash
./13_dump_aql_queues_gdb.sh --pid 875176
./13_dump_aql_queues_gdb.sh --pid=875176 -o /tmp/queue_dump
```

Output directory contents:

```text
aql_queue_dump/
├── aql_packet_decode_gdb.txt
└── amd-smi-pre-attach.txt
```

`amd-smi-pre-attach.txt` contains the attempted command, merged complete
stdout/stderr, exit codes, timeout, timestamps, and final status. The script
uses `sudo -n` when not already root and may retry the same absolute executable
directly after an immediate sudo failure. Missing tools, sudo denial, command
failure, and timeout are recorded without preventing the GDB dump.

Optional environment variables:

- `GDB`: GDB with Python support (default: `/usr/bin/gdb`)
- `GDB_TIMEOUT`: attach/decode timeout in seconds (default: `120`)
- `AMD_SMI_TIMEOUT`: pre-attach `/opt/rocm/bin/amd-smi` timeout (default: `15`)
- `LD_PRELOAD`: set by the caller when debug ROCr symbols are required
- `SOLIB_SEARCH_PATH`, `DEBUG_FILE_DIRECTORY`: forwarded to GDB when needed

See [Optional: LD_PRELOAD and upgraded GDB](#optional-ld_preload-and-upgraded-gdb)
below for local debug-runtime and DWARF 5 setup.

### `00_pull_build_gdb.sh`

Build upstream GNU GDB with Python 3.11 into a local prefix. This is optional
and is not required by `06_collect_all_gpu_debug.sh` or
`13_dump_aql_queues_gdb.sh`.

```bash
./00_pull_build_gdb.sh
./00_pull_build_gdb.sh --prefix /path/to/gdb --python /usr/bin/python3.11
./00_pull_build_gdb.sh --force-rebuild
./00_pull_build_gdb.sh --install-bashrc
```

Default install location:

```text
tools/gdb-14.2/bin/gdb
```

After a successful build, the script prints an optional `export GDB=...` line.
Script `13` still defaults to `/usr/bin/gdb` unless you set `GDB`; it does not
require or auto-select the locally built GDB.

Use it for the current shell:

```bash
export GDB=/path/to/your/gdb-with-python
./13_dump_aql_queues_gdb.sh --pid 875176
```

To persist across logins, append the export to `~/.bashrc` manually, or run:

```bash
./00_pull_build_gdb.sh --install-bashrc
```

Re-run with `--force-rebuild` to upgrade GDB in the same prefix later.

## Optional: LD_PRELOAD and upgraded GDB

These are optional enhancements for local debugging with debug-symbol ROCr/HIP
builds. Neither `06_collect_all_gpu_debug.sh` nor `13_dump_aql_queues_gdb.sh`
sets them automatically.

### When to use `LD_PRELOAD`

Use `LD_PRELOAD` when the target process was started with a local debug
`libhsa-runtime64.so` from `rocm-systems/build`, for example:

```bash
export LD_PRELOAD=/path/to/rocm-systems/build/airuntime-2291/rocr/rocr/lib/libhsa-runtime64.so.1.21.0
./13_dump_aql_queues_gdb.sh --pid 875176
```

For one-click collection with script `06`:

```bash
LD_PRELOAD=/path/to/libhsa-runtime64.so.1.21.0 \
  ./06_collect_all_gpu_debug.sh --pid 875176
```

Script `13` does not modify the target runtime environment. `LD_PRELOAD` only
affects the GDB process launched by the dump script.

Development/test wrapper:

```bash
./tests/dump_aql_queues_with_build_preload.sh --pid 875176
```

### When to upgrade GDB

Optional troubleshooting only: script `13` has no minimum GDB version
requirement. Older system GDB builds may print harmless DWARF warnings for
HIP/CLR libraries built with newer debug info, for example:

```text
Dwarf Error: DW_FORM_strx1 found in non-DWO CU [in module .../libamdhip64.so.7]
```

If queue enumeration still succeeds, the dump is usually usable. To remove the
warning or improve DWARF 5 compatibility, build and use a newer GDB:

```bash
./00_pull_build_gdb.sh
export GDB=/path/to/your/gdb-with-python   # path printed by 00_pull_build_gdb.sh
./13_dump_aql_queues_gdb.sh --pid 875176
```

Optional persistence in `~/.bashrc` (only if you choose):

```bash
./00_pull_build_gdb.sh --install-bashrc
# or manually:
# export GDB=/path/to/your/gdb-with-python
```

Notes:

- `/opt/rocm/bin/rocgdb-py3.11` currently crashes when sourcing
  `aql_packet_decode_gdb.py`; a locally built upstream GDB with Python support
  is a known workaround.
- Building GDB requires `python3.11-devel` and other build dependencies.
- If `GDB` is not set, script `13` defaults to `/usr/bin/gdb` (must support
  Python); it may fall back to another `gdb` on `PATH` with Python support.
- `00_pull_build_gdb.sh` never modifies `~/.bashrc` unless you pass
  `--install-bashrc`.

## Debug bundle layout

A typical unified collection directory:

```text
gpu_debug_bundle_<HOST>_<TIMESTAMP>/
├── queue_doctor/
│   ├── target-gpus.json
│   ├── report.json
│   ├── pending/
│   │   └── snapshot-<NN>-queue-<UMR_ID>-<type>.txt
│   └── full-ring/
│       ├── snapshot-<NN>-queue-<UMR_ID>-<type>.bin
│       └── snapshot-<NN>-queue-<UMR_ID>-<type>.decoded.txt
├── aql_queue_dump/
├── kfd/
├── cpc/
├── waves/
├── sdma_registers.txt
├── system_info.txt
├── collector.txt
├── commands.txt
└── summary.txt
```

The `full-ring/` directory and both files are present by default after a
successful capture and decode. `--discard-full-ring-raw` removes the `.bin`
after success; `--no-full-ring` disables both full-ring artifacts. For multiple
selected GPUs, each device has its own Queue Doctor directory with the same
`pending/` and `full-ring/` layout. The actual layout depends on the enabled
collectors, target PIDs, and GPU count. Most standalone scripts also produce a
command log, error log, summary, and `.tgz` archive.

For unified collection, the complete target-GPU inventory is available in the
bundle `summary.txt` and `collector.txt`; Queue Doctor also writes it to
`queue_doctor/target-gpus.json` and `queue_doctor/report.json` (or the
corresponding per-device directory for multi-GPU capture).

The bundle `system_info.txt` records the running kernel, loaded `amdgpu`/KFD
module metadata from sysfs and `modinfo`, and the complete `dkms status`
output. Missing modules or commands are recorded with explicit unavailable
markers instead of failing the collection.

## Common environment variables

- `DEBUG_TOOLS_TMP_DIR`: default output root for all tools
- `UMR`: UMR executable or source directory
- `OUTPUT_DIR`: output directory for selected collectors
- `LOG_FILE`: output path for single-file collectors
- `AMD_SMI`: preferred path to the `amd-smi` executable; collectors otherwise
  search `PATH` and standard ROCm install locations such as `/opt/rocm/bin`
- `DMESG`: path to the `dmesg` executable
- `GDB`: GDB with Python support for `13_dump_aql_queues_gdb.sh` (default: `/usr/bin/gdb`)
- `GDB_TIMEOUT`: attach/decode timeout for script `13` (default: `120`)
- `AMD_SMI_TIMEOUT`: pre-attach `/opt/rocm/bin/amd-smi` timeout (default: `15`)
- `LD_PRELOAD`: optional preload for script `13` or `06` when using debug ROCr

Options differ between scripts. Check the corresponding help before running a
collector:

```bash
./06_collect_all_gpu_debug.sh --help
python3 02_umr_queue_doctor.py --help
```

## Safety notes

1. Customer logs, full rings, GPUVM addresses, and process information may
   contain sensitive data. Do not commit them to Git or upload them to public
   services.
2. A full ring includes historical packets that have already been consumed but
   not overwritten. Use the current RPTR/WPTR to identify live packets; do not
   treat every retained packet as pending.
3. CPC collection, wave halting, dynamic debug, and tracing can change system
   debug state or add runtime overhead. Use them in a controlled environment
   and do not leave them enabled on production nodes.
4. Collection commands use bounded timeouts. When a step fails, inspect its
   error log and command log first.
5. On multi-XCC or partitioned systems, ensure that `--vmp` or
   `--vm-partition` matches the partition containing the target queue.

## Troubleshooting

### UMR cannot be found

Specify it through an argument or environment variable:

```bash
UMR=/path/to/umr ./08_dump_all_gpu_waves.sh
python3 02_umr_queue_doctor.py --umr-bin /path/to/umr --pid 4008481
```

### KFD queues cannot be found for a PID

Confirm that the process is still running, then inspect:

```bash
ls /sys/class/kfd/kfd/proc/<PID>/queues
```

KFD queue and GPUVM metadata is released when the process exits, so a live ring
dump is no longer possible afterward.

### debugfs cannot be read

Confirm that debugfs is mounted and that the current user has `sudo` access:

```bash
sudo mount -t debugfs none /sys/kernel/debug
```

### GDB reports DWARF errors during AQL dump

If queue enumeration still succeeds (`Enumerated N queue(s): ...`), the dump is
usually valid and the message is often a HIP/CLR debug-info compatibility
warning on older system GDB versions. To suppress it, build and use a newer GDB:

```bash
./00_pull_build_gdb.sh
GDB=/path/to/your/gdb-with-python ./13_dump_aql_queues_gdb.sh --pid 875176
```

If enumeration fails together with the DWARF error, verify that the mapped
`libhsa-runtime64.so` contains `.debug_info` and retry with a GDB that has
Python support (optionally a newer build from `00_pull_build_gdb.sh`).

### AQL dump finds no queues

Confirm the target PID is still running and, for local debug ROCr builds, set
`LD_PRELOAD` to the same debug `libhsa-runtime64.so` used by the workload before
running script `13` or `06`.

## License

See [LICENSE](LICENSE).

## Author

Perry Yuan — <Perry_Yuan@outlook.com>
