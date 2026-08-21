# UMR CPC log parser

`parse_cpc.py` parses text produced by UMR `--tool cpc`. It uses only the
Python standard library and is tested with the installed Python 3.6.

The log server's `/__dashboard__/` route imports this module directly and uses
`discover_logs`, `scan_bundle_metadata`, and `parse_log`; it does not shell out
or alter the standalone CLI/schema. Dashboard joins still require exact
`MQD_BASE` equality within independently known physical-GPU/XCC context.

The parser accepts one log, a CPC directory, or a complete debug bundle. It
keeps raw register/MQD text and source line numbers even when the ASIC is not
known. ASIC-specific decoding is enabled only when sibling kernel metadata
identifies GFX 9.4.3, or when the profile is selected explicitly.

## Usage

```bash
./parse_cpc.py LOG_OR_DIRECTORY
./parse_cpc.py BUNDLE --mqd 0x01795000
./parse_cpc.py BUNDLE --json --output cpc-report.json
./parse_cpc.py isolated.log --profile gc_9_4_3
./parse_cpc.py unknown.log --profile raw
```

Directory input recursively selects files named `*umr_cpc*.txt`; wave,
kernel, and other text captures are not parsed as CPC logs. Sibling
`*commands*.txt` and `*kernel*.txt` files are used as metadata:

- `gpuN` in a filename remains `filename_gpu_index`.
- UMR `-i` remains `umr_instance`.
- UMR `-vmp` is the authoritative XCC context when available.
- A DRM kernel line can map the UMR instance/minor to a PCI BDF.

These namespaces are deliberately not equated. For example, the known bundle
labels one capture `gpu3`, while its command uses `-i 1`; kernel metadata maps
DRM minor/UMR instance 1 to PCI `0000:0a:00.0`.

## MQD lookup

`--mqd ADDRESS` compares the address directly with the displayed `MQD_BASE`.
It does **not** shift the address. A match is accepted only for an HQD section
that UMR emitted after its live `CP_HQD_ACTIVE` test, and it remains scoped to
that file's GPU/XCC context.

The lookup reports:

- `zero`: no exact active match (exit 3)
- `unique`: one exact active match (exit 0)
- `multiple`: more than one exact active match (exit 4)

Saved values in an inactive MQD can be stale. If XCC metadata is unavailable,
the report says that same-XCC identity could not be independently confirmed.

Known artifact example:

```text
0x1795000 -> me1.pipe0.queue2
```

## Verified GC 9.4.3 formulas

The `gc_9_4_3` profile is based on the local UMR
`database/ip/gc_9_4_3.reg`, Linux `gc_9_4_3_sh_mask.h`,
`v9_structs.h`, `kfd_mqd_manager_v9.c`,
`amdgpu_amdkfd_gc_9_4_3.c`, and UMR's `print_cpc.c`/`print_cpc.h`.

- MQD base: use UMR's displayed `MQD_BASE` directly.
- PQ/ring base: UMR already reconstructs `PQ_BASE` from the hardware fields.
- `QUEUE_SIZE = PQ_CONTROL[5:0]`.
- Ring dwords: `1 << (QUEUE_SIZE + 1)`.
- Ring bytes: `ring_dwords * 4`.
- 64-byte AQL slots: `ring_bytes / 64`.
- VMID: `CP_HQD_VMID[3:0]`.
- IB VMID: `CP_HQD_VMID[11:8]`.
- AQL-compatible slot mapping is enabled only when
  `SLOT_BASED_WPTR == 2`, the value programmed by GC9 KFD for AQL queues.
- Hardware PQ pointers are retained as dword offsets. Their physical dword is
  `raw_pointer % ring_dwords`; a 64-byte packet slot is
  `dword_offset // 16`.

If a pointer is not on a 16-dword packet boundary, the parser still reports
its physical slot and intra-slot dword, but does not claim a valid pending
packet interval. A wrapped interval is reported only when both pointers are
available and packet-aligned.

`DOORBELL_BIF_DROP` is reported as a configured policy bit. It is never
presented as evidence of an observed dropped doorbell. PASID remains `null`
unless a future input contains a separately verified source; it is not
inferred from VMID.

## Interpretation limits

UMR reads registers and MQD memory sequentially. The result is not atomic, so
pointer disagreement or an in-flight packet can be a capture race. The text
output distinguishes active-empty, active-nonempty, and unknown states, but
never declares a queue hung from one snapshot.

Queue sections are active at the time UMR tests them. MQD memory may change
before or after the register reads. Missing registers, failed MQD VRAM reads,
malformed rows, and truncated files produce warnings rather than exceptions.

## JSON schema and exit status

`--json` emits schema `umr-cpc-report` version `1.0`. Top-level keys are:

```text
schema_name, schema_version, tool, input, summary, logs, mqd_query, warnings
```

Every register record contains `name`, `raw`, numeric `value`, `line`, and
`element_index`. Every MQD dword contains `index_dword`, `index_hex`, `raw`,
numeric `value`, and `line`.

Exit codes:

- 0: parse completed, or `--mqd` found one unique match
- 1: input/read/write error or no matching CPC logs
- 2: command-line usage error
- 3: `--mqd` found no active exact match
- 4: `--mqd` found multiple active exact matches

Warnings and truncated input do not make an otherwise completed parse fail.

## Tests

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile parse_cpc.py tests/test_parse_cpc.py
```

Fixtures are intentionally small snippets; no full customer logs or generated
reports are checked in.
