# ROCm Forensics Toolkit

**简体中文** | [English](README.md)

> AMD GPU queue、KFD、SDMA、CPC、Wave 与 ROCclr 运行时故障采集工具集。

本项目提供一组面向 AMD GPU hang、queue stall、signal wait、SDMA timeout
和 firmware 调试场景的采集脚本。工具会关联进程、KFD Queue、GPU 拓扑节点
与 PCI BDF，并将诊断数据保存为可归档、可离线分析的结构化 debug bundle。

## 主要能力

- 自动发现目标进程使用的 GPU 和 KFD Queue
- 多快照采集 Queue RPTR/WPTR、MQD 与 pending packet
- 采集 KFD runlist、HQD、MQD、fence、trace 和 dmesg
- 读取所有 GPU/XCC 的 CPC、Wave 与 SDMA 状态
- 从 Docker 容器收集 ROCclr/AMD runtime 日志
- 按 PID 导出完整保留的 user queue ring
- 读取进程 GPU 虚拟内存和 Queue 中可见的 HSA signal 值
- 按 DRM category 和内核源码文件选择性开启 debug 日志
- 自动生成带时间戳的目录、汇总文件和压缩包

## 环境要求

- Linux AMDGPU/KFD 驱动
- Python 3.8 或更高版本
- Bash 4.4 或更高版本
- [UMR](https://gitlab.freedesktop.org/tomstdenis/umr)
- `amd-smi`
- `sudo` 权限（读取 debugfs、KFD 和部分硬件状态时需要）
- 可选：Docker、CMake、RPM/CPack 工具

建议在专用调试节点运行。目标 GPU 进程必须仍在运行，才能读取其 KFD Queue
元数据和 GPUVM ring 内容。

## 快速开始

### 1. 构建 UMR

请使用普通用户运行，脚本只在安装依赖时调用 `sudo`：

```bash
./00_pull_build_umr.sh
```

默认从 UMR upstream `main` 分支构建。生成 RPM：

```bash
./00_pull_build_umr.sh --package
```

### 2. 一键采集完整 debug bundle

必须指定目标进程 PID：

```bash
./06_collect_all_gpu_debug.sh --pid 4008481
```

默认会采集完整 ring。若需关闭：

```bash
./06_collect_all_gpu_debug.sh --pid 4008481 --no-full-ring
```

成功采集的 raw `.bin` 也会默认保留。若成功解码后只保留 decoded report：

```bash
./06_collect_all_gpu_debug.sh --pid 4008481 --discard-full-ring-raw
```

默认输出到：

```text
/tmp/debug-tools-<UID>/gpu_debug_bundle_<HOST>_<TIMESTAMP>/
```

通过环境变量修改默认根目录：

```bash
DEBUG_TOOLS_TMP_DIR=/var/tmp/gpu-debug \
  ./06_collect_all_gpu_debug.sh --pid 4008481
```

## 脚本说明

### `00_pull_build_umr.sh`

拉取并构建 UMR，支持 LLVM 自动检测、无 LLVM 回退、依赖安装和 RPM 打包。

```bash
./00_pull_build_umr.sh --install-deps
./00_pull_build_umr.sh --branch main --package
```

### `02_umr_queue_doctor.py`

核心 Queue 诊断工具。采集多次 Queue 快照并分析 RPTR/WPTR 是否推进、
head packet、pending packet、MQD 状态和数据源冲突。

默认 full-ring 会同时保留 raw `.bin` 和 `.decoded.txt`。UMR 的
`rb_buf_size` 单位是 32-bit word，Queue Doctor 会乘以 4 得到 ring 的 byte
容量；后者扫描每个物理 64-byte slot，并明确覆盖逻辑 pending 区间
`[RPTR, WPTR)`（包括 wraparound）映射到的所有物理 slot。pending slot 的
header 带有 `[PENDING logical=N]` 标记，报告同时记录预期、已解码和缺失的
pending slot，避免静默遗漏。输出采用与 pending dump 一致的 UMR 风格：先输出
`Dumping ...`、`Decoding IB at ...`，再按
`[vmid@base + offset] [raw header] Opcode ... [HSA_*]` 输出 packet，
body/signal 字段使用带 packet 内相对 byte offset 的 `|---> field=value`
缩进。decoded text 不再重复显示每个 slot 的完整 64-byte payload；精确 ring
bytes 仍保留在 raw `.bin` 产物中。Queue/HQD/doorbell、RPTR/WPTR 和 dispatch
元数据仍保留在 report header。文本不使用 JSONL。
header 类型为 INVALID 时，主 packet 类型始终显示为权威的 `HSA_INVALID`；
内部残留 body 启发式解析仍可解码有用的下级字段，但 decoded text 不显示残留
body 类型注释。这些字段不改变 header 类型，也不声称 packet 已消费、归属或
处于当前状态。非零 barrier dependency handle 使用有界、缓存的读取：可读时
显示 `value` 和 `status=PENDING|COMPLETED`；不可读时只显示
handle。值为零的 barrier dependency handle 表示无依赖或已满足，所以 full-ring
文本只显示 `dep_signal[N]=0x0`。可读的非零 completion/watch signal 同样显示
value 和 `PENDING`/`COMPLETED` status；handle 为空或不可读时只显示 handle，
不显示 unavailable status 或 reason 行。结构化 slot 数据保留在 `report.json`。
`summary.txt` 和 `report.json` 会列出这两个产物及保留策略。使用
`--discard-full-ring-raw` 时，仅在成功解码后删除 `.bin`。即使指定该选项，
解码失败或不完整时仍会保留已有 raw data，并在 decoded report 中记录原因。
`--keep-full-ring-raw` 为向后兼容继续接受，但与默认行为相同，无需再指定。
报告还会记录 GPU PCI、KFD gpu_id、UMR-local queue selector、VM
partition/XCC、GFX hub 和 VMID 上下文。如果 pending 区间的所有权威 header
都是 `HSA_INVALID`，工具会提示对照 pending dump 并检查这些上下文，但不会为了
满足覆盖要求而重命名 packet 类型。

```bash
python3 02_umr_queue_doctor.py --pid 4008481
python3 02_umr_queue_doctor.py --pid 4008481 --gpu-id 0 1
python3 02_umr_queue_doctor.py \
  --pid 4008481 --queue 28 --samples 3 --interval 10
python3 02_umr_queue_doctor.py \
  --pid 4008481 --discard-full-ring-raw
```

Queue Doctor 会在持久化报告中保留权威的 PID 目标 GPU inventory 和已选择 GPU
诊断信息，但正常独立采集时不再向终端输出冗长的 source-of-truth、逐 GPU
inventory 或 Queue namespace 区块。通过 `06_collect_all_gpu_debug.sh` 集成
运行时，inventory 预检不会产生可见的 collector `BEGIN`/`END` 区块；其输出仍
写入 `summary.txt` 和 `collector.txt`，Queue Doctor 也会在
`target-gpus.json` 和 `report.json` 中保留这些元数据。系统可见但不在该 PID
的 KFD Queue inventory 中的 GPU 仍会在内部过滤，不产生逐 GPU 输出或 dump
文件。持久化的 inventory 开头如下：

```text
Target GPUs
===========
  AMD-SMI=4 / KFD node=6 / gpu_id=61984 / PCI=0001:0b:00.0 / renderD160 / KFD queues=2 / KFD queue IDs=[41, 77]

Queue Doctor selected GPUs:
  DUMP AMD-SMI=4 / KFD node=6 / gpu_id=61984 / PCI=0001:0b:00.0 / renderD160
```

未指定 `--pid` 时，默认通过 `amd-smi` 查找 `torch_server.bin`。

### `03_dump_sdma_registers.sh`

只读采集所有 GPU 和 SDMA instance 的 status、GFX、PAGE、RLC 与 XNACK
寄存器，不写入硬件寄存器。

在任何寄存器读取之前，脚本会解析 `UMR`（可执行文件或源码目录），将 UMR
路径与版本写入日志头，并检查所选 UMR 版本是否 **>= 1.0.11**，以及是否支持本
采集器使用的命令（`--script instances`、`-lb`、`-vmp`、`-O`、`-r`）。版本过旧
或不完整的 UMR 会提前失败并给出可操作的错误提示。需要时可运行
`./00_pull_build_umr.sh` 构建或更新 UMR，或设置 `UMR=/path/to/newer/umr`。

每个 GPU 对所有已发现的 SDMA block **合并为一次 UMR 批量读取**（`-lb` 仍按
GPU 单独执行，因为 SDMA block 名称与拓扑相关，且不会跨 GPU 缓存）。跨 GPU
采集默认同时运行所有已发现的 GPU；可用 `--jobs N` 限制并发数，
`--jobs 1` 为顺序模式。每次 UMR 调用都有超时（`--timeout` /
`UMR_TIMEOUT_SEC`，默认 **600** 秒），避免单个 UMR 挂死拖住全局。日志会记录
并行度、超时、UMR 调用策略/次数，以及每个 GPU worker 的开始/结束时间与耗时。

```bash
./03_dump_sdma_registers.sh
./03_dump_sdma_registers.sh --vmp 1 -o /tmp/sdma-registers.txt
./03_dump_sdma_registers.sh --jobs 4 --timeout 900
```

离线回归测试：`./tests/run_sdma_register_dump_test.sh`

### `04_dump_kfd_snapshots.sh`

有限次数采集 KFD runlist、HQD、MQD、AMD-SMI、fence、trace 和 dmesg，
不会无限跟随 `trace_pipe` 或 `dmesg`。

```bash
./04_dump_kfd_snapshots.sh --snapshots 3 --interval 20
./04_dump_kfd_snapshots.sh --dynamic-debug
```

### `15_trace_kfd_queue.sh`

独立的 KFD queue/runlist dynamic-debug 工具。按 file、format string 或 func
name（而非行号）启用 KFD 源码中的选定 `printk` callsite，并设置
`amdgpu.debug_evictions=1` 和 `amdgpu.halt_if_hws_hang=1`，可选跟随内核
journal。eviction worker 函数规则使用 `+pfl`，其余规则使用 `+p`。该脚本**不会**
被 `06_collect_all_gpu_debug.sh` 调用。

```bash
./15_trace_kfd_queue.sh status
./15_trace_kfd_queue.sh enable
./15_trace_kfd_queue.sh capture ./kfd-queue.txt
./15_trace_kfd_queue.sh disable
```

要求：

- `CONFIG_DYNAMIC_DEBUG`（`/proc/dynamic_debug/control` 或 debugfs 等价路径）
- `sudo` 权限
- 已加载带 `debug_evictions` 和 `halt_if_hws_hang` sysfs 参数的 `amdgpu`
  模块

`capture` 会先 enable，再将 `journalctl -k -f` tee 到 LOG，按 Ctrl-C 停止；
停止后 debug 仍保持开启，需显式运行 `disable`。

### `05_dump_all_cpc_info.sh`

采集所有 GPU/XCC 的 CPC、Wave、KFD runlist、firmware、module parameter
和 kernel log，并生成 `.tgz`。

```bash
./05_dump_all_cpc_info.sh
```

该脚本会检查并可能启用 `amdgpu.halt_if_hws_hang`。仅应在受控调试环境使用。

CPC collector 只探测一次 UMR help，并按安装版本选择命令：当前 UMR 使用
`--tool cpc --`，2026 年 7 月 tool interface 变更前的版本使用
`--print-cpc`。每个 GPU/XCC 的 CPC stdout 都会写入文件；若接口不受支持，
脚本会在逐 XCC 采集前失败，不会重复刷 unknown-argument warning。

### `06_collect_all_gpu_debug.sh`

推荐入口。按顺序运行 Queue Doctor、SDMA、KFD、CPC 和 Wave collector，
最后生成一个统一 debug bundle。必须显式指定 `--pid`，不会自动发现目标 PID。

```bash
./06_collect_all_gpu_debug.sh --pid 4008481
./06_collect_all_gpu_debug.sh \
  --pid 4008481 --samples 3 --interval 5 --vmp 0
```

可使用 `--skip-queue`、`--skip-sdma`、`--skip-kfd`、`--skip-cpc`、
`--skip-waves` 或 `--skip-aql-queue` 禁用单项采集。
Queue Doctor 成功采集的 `.bin` 默认保留；`--discard-full-ring-raw` 会向
Queue Doctor 传递成功解码后删除 raw 的选项。旧的
`--keep-full-ring-raw` 仍会转发，但与默认行为相同。

启用后会额外生成 AQL/SDMA gdb dump，保存在 `aql_queue_dump/`（多 PID 时为
`aql_queue_dump_pid<PID>/`）。如需自定义 GDB 或 preload，请在运行 `06` 前
自行 `export GDB=...` 或 `export LD_PRELOAD=...`。
Queue Doctor 仍会按 bundle 结构创建 dump 目录，并保留最终状态/数量诊断，
但不会单独打印每个目录的路径行。

每个实际调用的 collector（包括目标 inventory 和 AQL GDB）都会记录整数秒耗时；
环境支持时使用单调时钟。普通可见 collector 步骤和 `collector.txt` 使用以下
固定完成格式：

```text
[collector] END queue_doctor: PASS elapsed=12s
[collector] END cpc_dump: FAIL rc=17; continuing elapsed=3s
[collector] END aql_queue_dump: FAIL rc=124; continuing elapsed=120s
```

目标 inventory 预检的 `BEGIN`/`END` 和耗时只记录在
`collector.txt`/`summary.txt`，不输出到终端。

`summary.txt` 中每个已调用步骤的第 4 个制表符分隔字段会保存相同的
`elapsed=<N>s`；跳过的步骤使用 `-`。`total_elapsed=<N>s` 和
`[collector] TOTAL elapsed=<N>s` 记录整个 wrapper 的总耗时，启用归档时包含
归档创建时间。Queue Doctor 已有的 pending/full-ring 内层耗时仍独立保存在其
自身产物中（如有）；这些操作已经包含在外层 `queue_doctor` 耗时里，不应再次
相加。

### `08_dump_all_gpu_waves.sh`

对所有 UMR GPU/XCC 采集多次 Wave 快照。

```bash
./08_dump_all_gpu_waves.sh --samples 5 --interval 5
./08_dump_all_gpu_waves.sh --halt-waves
```

`--halt-waves` 具有侵入性，只能在专用调试系统使用。

### `09_collect_docker_amd_log.sh`

发现运行目标进程的 Docker 容器，复制 AMD runtime 日志并生成压缩包。

```bash
./09_collect_docker_amd_log.sh
./09_collect_docker_amd_log.sh --container my-container
./09_collect_docker_amd_log.sh --pid 138732
```

### `10_dump_full_user_queue_rings.sh`

按 PID 从 KFD sysfs 自动发现 Queue，通过 Queue `gpuid` 精确映射 PCI GPU，
然后使用 UMR `use_full_user_queue` 导出完整保留 ring。

```bash
./10_dump_full_user_queue_rings.sh --pid 875176
./10_dump_full_user_queue_rings.sh --pid 875176 --queue 28
./10_dump_full_user_queue_rings.sh \
  --pid 875176 --pci 0000:0a:00.0 --vm-partition 0
```

### `11_configure_drm_debug.sh`

选择性开启或关闭 `drm.debug` category，以及指定内核源码文件的
dynamic-debug callsite。脚本只修改明确指定的 category 和文件。

```bash
./11_configure_drm_debug.sh enable --drm driver,kms \
  --file amdgpu_job.c --file kfd_chardev.c
./11_configure_drm_debug.sh disable --drm kms --file amdgpu_job.c
./11_configure_drm_debug.sh show --file kfd_chardev.c
```

文件通配符需要使用引号，例如
`--file 'drivers/gpu/drm/amd/amdgpu/*.c'`。配置会保持到手动关闭、再次修改
或系统重启；可先加 `--dry-run` 检查将要执行的写操作。

### `12_read_process_gpu_memory.py`

将 UMR 绑定到仍在运行的 KFD 进程，读取单个 GPU 虚拟地址、左闭右开的地址
范围、一个或多个 HSA signal handle，或者从进程 Queue packet 中发现 signal
并读取其值。

```bash
./12_read_process_gpu_memory.py --pid 1234 --address 0x7f0000001000
./12_read_process_gpu_memory.py --pid 1234 \
  --range 0x7f0000001000 0x7f0000001100
./12_read_process_gpu_memory.py --pid 1234 \
  --signal 0x7f0000123400
./12_read_process_gpu_memory.py --pid 1234 --all-signals
./12_read_process_gpu_memory.py --pid 1234 --all-signals --full-ring
```

`--all-signals` 会报告非零的 `dep_signal`、`completion_signal`，以及 SDMA
poll/atomic packet 中可以识别为 HSA signal 的地址。这表示采集到的 Queue 中
可见的全部 signal，不包括 ROCr 内部已分配但没有出现在 Queue packet 中的
signal。`--full-ring` 还会扫描历史保留 packet，因此部分 handle 可能已经销毁，
显示为 `invalid/stale`。

### `13_dump_aql_queues_gdb.sh`

Attach GDB 前，先对固定可执行文件 `/opt/rocm/bin/amd-smi` 进行有超时、
非交互式采集；然后 Attach 普通 GDB 到 live 进程，通过
`aql_packet_decode_gdb.py` 解码 HSA AQL queue 和 SDMA blit ring。该脚本
不会启动 workload，也不会修改目标进程内的 ROCm 运行时环境。

要求：所选 GDB 必须可执行且支持 Python 脚本。脚本 `13` 不检查、也不要求
最低或特定 GDB 版本。默认 `/usr/bin/gdb`；可用 `GDB` 或 `--gdb` 覆盖。

```bash
./13_dump_aql_queues_gdb.sh --pid 875176
./13_dump_aql_queues_gdb.sh --pid=875176 -o /tmp/queue_dump
```

输出目录示例：

```text
aql_queue_dump/
├── aql_packet_decode_gdb.txt
└── amd-smi-pre-attach.txt
```

`amd-smi-pre-attach.txt` 包含尝试执行的命令、合并后的完整 stdout/stderr、
退出码、超时、时间戳和最终状态。非 root 用户使用 `sudo -n`；sudo 立即失败
时，可以直接重试同一个绝对路径。工具缺失、sudo 拒绝、命令失败或超时都会被
记录，但不会阻止后续 GDB dump。

可选环境变量：

- `GDB`：支持 Python 的 GDB（默认：`/usr/bin/gdb`）
- `GDB_TIMEOUT`：attach/decode 超时秒数（默认：`120`）
- `AMD_SMI_TIMEOUT`：attach 前 `/opt/rocm/bin/amd-smi` 超时（默认：`15`）
- `LD_PRELOAD`：需要 debug ROCr 符号时由调用方自行设置
- `SOLIB_SEARCH_PATH`、`DEBUG_FILE_DIRECTORY`：按需传给 GDB

本地 debug runtime 与 DWARF 5 兼容说明见
[可选：LD_PRELOAD 与升级 GDB](#可选ld_preload-与升级-gdb)。

### `00_pull_build_gdb.sh`

将 upstream GNU GDB（带 Python 3.11 支持）编译安装到本地 prefix。这是可选项，
`06_collect_all_gpu_debug.sh` 和 `13_dump_aql_queues_gdb.sh` 都不依赖它。

```bash
./00_pull_build_gdb.sh
./00_pull_build_gdb.sh --prefix /path/to/gdb --python /usr/bin/python3.11
./00_pull_build_gdb.sh --force-rebuild
./00_pull_build_gdb.sh --install-bashrc
```

默认安装路径：

```text
tools/gdb-14.2/bin/gdb
```

构建成功后，脚本会打印可选的 `export GDB=...` 用法。未设置 `GDB` 时，
脚本 `13` 仍默认使用 `/usr/bin/gdb`；不会要求或自动选用本地编译的 GDB。

当前 shell 临时使用：

```bash
export GDB=/path/to/your/gdb-with-python
./13_dump_aql_queues_gdb.sh --pid 875176
```

如需登录后自动生效，可手动追加到 `~/.bashrc`，或运行：

```bash
./00_pull_build_gdb.sh --install-bashrc
```

后续可用 `--force-rebuild` 在同一 prefix 下升级 GDB。

## 可选：LD_PRELOAD 与升级 GDB

以下内容为本地 debug 场景的增强项，适用于带 debug symbol 的 ROCr/HIP build。
`06_collect_all_gpu_debug.sh` 和 `13_dump_aql_queues_gdb.sh` 都不会自动设置
这些变量。

### 何时使用 `LD_PRELOAD`

当目标进程使用 `rocm-systems/build` 下的 debug `libhsa-runtime64.so` 启动时，
dump 前可自行设置：

```bash
export LD_PRELOAD=/path/to/rocm-systems/build/airuntime-2291/rocr/rocr/lib/libhsa-runtime64.so.1.21.0
./13_dump_aql_queues_gdb.sh --pid 875176
```

一键采集时同样由调用方传入：

```bash
LD_PRELOAD=/path/to/libhsa-runtime64.so.1.21.0 \
  ./06_collect_all_gpu_debug.sh --pid 875176
```

脚本 `13` 不会修改目标进程 runtime 环境；`LD_PRELOAD` 只作用于 dump 脚本
启动的 GDB 进程。

开发/测试 wrapper：

```bash
./tests/dump_aql_queues_with_build_preload.sh --pid 875176
```

### 何时升级 GDB

以下为可选排查手段：脚本 `13` 没有最低 GDB 版本要求。较旧的系统 GDB
读取较新的 HIP/CLR debug info 时，可能打印 DWARF 警告，例如：

```text
Dwarf Error: DW_FORM_strx1 found in non-DWO CU [in module .../libamdhip64.so.7]
```

如果 queue 枚举仍然成功，dump 通常可用。若要消除警告或改善 DWARF 5 兼容性，
可编译并使用较新的 GDB：

```bash
./00_pull_build_gdb.sh
export GDB=/path/to/your/gdb-with-python   # 路径由 00_pull_build_gdb.sh 打印
./13_dump_aql_queues_gdb.sh --pid 875176
```

可选：写入 `~/.bashrc`（仅在你主动选择时）：

```bash
./00_pull_build_gdb.sh --install-bashrc
# 或手动追加：
# export GDB=/path/to/your/gdb-with-python
```

说明：

- `/opt/rocm/bin/rocgdb-py3.11` 在 source `aql_packet_decode_gdb.py` 时会
  崩溃；本地编译的 upstream GDB（带 Python 支持）是已知 workaround。
- 编译 GDB 需要 `python3.11-devel` 等 build 依赖。
- 未设置 `GDB` 时，脚本 `13` 默认使用 `/usr/bin/gdb`（须支持 Python）；若
  不可用，可能回退到 `PATH` 中其他支持 Python 的 `gdb`。
- `00_pull_build_gdb.sh` 不会自动修改 `~/.bashrc`，除非传入
  `--install-bashrc`。

## Debug bundle 结构

典型的一键采集目录：

```text
gpu_debug_bundle_<HOST>_<TIMESTAMP>/
├── queue_doctor/
│   ├── target-gpus.json
│   ├── report.json
│   ├── pending/
│   │   └── snapshot-<NN>-queue-<UMR_ID>-<type>.txt
│   └── full-ring/（默认启用）
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

成功采集并解码后，`full-ring/` 默认同时包含 `.bin` 和 `.decoded.txt`。
`--discard-full-ring-raw` 会在成功时删除 `.bin`，`--no-full-ring` 会关闭这两类
full-ring 产物。实际目录会根据启用的 collector、目标 PID 和 GPU 数量变化。
大部分独立脚本还会生成 command log、error log、summary 和 `.tgz` 归档。

一键采集时，完整目标 GPU inventory 可在 bundle 根目录的 `summary.txt` 和
`collector.txt` 中查看；Queue Doctor 还会写入
`queue_doctor/target-gpus.json` 和 `queue_doctor/report.json`（多 GPU 时查看
对应的设备目录）。

bundle 中的 `system_info.txt` 会记录当前内核、从 sysfs 和 `modinfo` 获取的
已加载 `amdgpu`/KFD 模块信息，以及完整的 `dkms status` 输出。模块或命令
不存在时会写入明确的 unavailable 标记，不会导致采集失败。

## 常用环境变量

- `DEBUG_TOOLS_TMP_DIR`：所有工具的默认输出根目录
- `UMR`：UMR 可执行文件或源码目录
- `OUTPUT_DIR`：部分 collector 的输出目录
- `LOG_FILE`：单文件 collector 的输出路径
- `AMD_SMI`：优先使用的 `amd-smi` 可执行文件路径；未配置时会继续搜索
  `PATH` 和 `/opt/rocm/bin` 等标准 ROCm 安装位置
- `DMESG`：`dmesg` 可执行文件路径
- `GDB`：脚本 `13` 使用的 GDB（须支持 Python；默认：`/usr/bin/gdb`）
- `GDB_TIMEOUT`：脚本 `13` 的 attach/decode 超时（默认：`120`）
- `AMD_SMI_TIMEOUT`：attach 前 `/opt/rocm/bin/amd-smi` 超时（默认：`15`）
- `LD_PRELOAD`：使用 debug ROCr 时，运行脚本 `13` 或 `06` 前可选设置

每个脚本的参数可能不同，请优先查看对应帮助：

```bash
./06_collect_all_gpu_debug.sh --help
python3 02_umr_queue_doctor.py --help
```

## 安全注意事项

1. 客户日志、完整 ring、GPUVM 地址和进程信息可能包含敏感数据，请勿直接提交到
   Git 或上传到公共服务。
2. Full ring 包含已经消费但尚未被覆盖的历史 packet。判断 live packet 时必须
   结合当前 RPTR/WPTR，不能把 ring 中所有内容都视为 pending。
3. CPC、Wave halt、dynamic debug 和 trace 会改变系统调试状态或增加运行开销。
   请在受控环境使用，并避免在生产节点长时间启用。
4. 所有采集命令都设置了有限 timeout；出现失败时先检查对应 error log 和
   command log。
5. 多 XCC 或 partition 系统应确认 `--vmp`/`--vm-partition` 与目标 Queue
   所在分区一致。

## 故障排查

### 找不到 UMR

通过参数或环境变量指定：

```bash
UMR=/path/to/umr ./08_dump_all_gpu_waves.sh
python3 02_umr_queue_doctor.py --umr-bin /path/to/umr --pid 4008481
```

### 找不到 PID 的 KFD Queue

确认进程仍在运行，并检查：

```bash
ls /sys/class/kfd/kfd/proc/<PID>/queues
```

进程退出后，KFD Queue 和 GPUVM 元数据会被释放，无法再执行 live ring dump。

### 无法读取 debugfs

确认 debugfs 已挂载且当前用户具有 `sudo` 权限：

```bash
sudo mount -t debugfs none /sys/kernel/debug
```

### AQL dump 出现 GDB DWARF 报错

若 queue 枚举仍然成功（`Enumerated N queue(s): ...`），dump 通常有效，这类
信息多见于旧版系统 GDB 读取 HIP/CLR debug info 时的兼容性警告。若要消除，
可编译并使用较新的 GDB：

```bash
./00_pull_build_gdb.sh
GDB=/path/to/your/gdb-with-python ./13_dump_aql_queues_gdb.sh --pid 875176
```

若 DWARF 报错同时导致 queue 枚举失败，请确认进程映射的 `libhsa-runtime64.so`
包含 `.debug_info`，并用支持 Python 的 GDB 重试（可选：通过
`00_pull_build_gdb.sh` 编译较新版本）。

### AQL dump 找不到 queue

确认目标 PID 仍在运行；若 workload 使用本地 debug ROCr build，请在运行脚本
`13` 或 `06` 前设置与 workload 相同的 debug `libhsa-runtime64.so` 到
`LD_PRELOAD`。

## License

详见 [LICENSE](LICENSE)。

## Author

Perry Yuan — <Perry_Yuan@outlook.com>
