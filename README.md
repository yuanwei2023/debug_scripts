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

Select a process by PID:

```bash
./06_collect_all_gpu_debug.sh --pid 4008481
```

Discover the PID by process name:

```bash
./06_collect_all_gpu_debug.sh --process-name torch_server.bin
```

Include full retained rings:

```bash
./06_collect_all_gpu_debug.sh --pid 4008481 --full-ring
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

```bash
python3 02_umr_queue_doctor.py --pid 4008481
python3 02_umr_queue_doctor.py --pid 4008481 --gpu-id 0 1
python3 02_umr_queue_doctor.py \
  --pid 4008481 --queue 28 --samples 3 --interval 10 --full-ring
```

When `--pid` is omitted, the tool uses `amd-smi` to find
`torch_server.bin`.

### `03_dump_sdma_registers.sh`

Read status, GFX, PAGE, RLC, and XNACK registers from every GPU and SDMA
instance. This collector is read-only and never writes hardware registers.

```bash
./03_dump_sdma_registers.sh
./03_dump_sdma_registers.sh --vmp 1 -o /tmp/sdma-registers.log
```

### `04_dump_kfd_snapshots.sh`

Collect a bounded number of KFD runlist, HQD, MQD, AMD-SMI, fence, trace, and
kernel-log snapshots. The script never follows `trace_pipe` or `dmesg`
indefinitely.

```bash
./04_dump_kfd_snapshots.sh --snapshots 3 --interval 20
./04_dump_kfd_snapshots.sh --dynamic-debug
```

### `05_dump_all_cpc_info.sh`

Collect CPC, wave, KFD runlist, firmware, module parameter, and kernel-log
state from every GPU/XCC, then create a `.tgz` archive.

```bash
./05_dump_all_cpc_info.sh
```

This script checks and may enable `amdgpu.halt_if_hws_hang`. Use it only in a
controlled debugging environment.

### `06_collect_all_gpu_debug.sh`

The recommended entry point. It runs Queue Doctor, SDMA, KFD, CPC, and wave
collectors sequentially and produces one unified debug bundle.

```bash
./06_collect_all_gpu_debug.sh --pid 4008481
./06_collect_all_gpu_debug.sh \
  --pid 4008481 --samples 3 --interval 5 --vmp 0
```

Use `--skip-queue`, `--skip-sdma`, `--skip-kfd`, `--skip-cpc`, or
`--skip-waves` to disable individual collectors.

### `07_demo_queue_auto_discovery.sh`

A compatibility entry point for queue auto-discovery tests. It forwards its
arguments to `tests/run_queue_auto_discovery_test.sh`.

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

## Debug bundle layout

A typical unified collection directory:

```text
gpu_debug_bundle_<HOST>_<TIMESTAMP>/
├── queue_doctor/
├── kfd/
├── cpc/
├── waves/
├── sdma-registers.log
├── amd-smi-process-discovery.log
├── system-info.log
└── collection-summary.tsv
```

The actual layout depends on the enabled collectors, target PIDs, and GPU
count. Most standalone scripts also produce a command log, error log, summary,
and `.tgz` archive.

## Common environment variables

- `DEBUG_TOOLS_TMP_DIR`: default output root for all tools
- `UMR`: UMR executable or source directory
- `OUTPUT_DIR`: output directory for selected collectors
- `LOG_FILE`: output path for single-file collectors
- `AMD_SMI`: path to the `amd-smi` executable
- `DMESG`: path to the `dmesg` executable

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

## License

See [LICENSE](LICENSE).

## Author

Perry Yuan — <Perry_Yuan@outlook.com>
