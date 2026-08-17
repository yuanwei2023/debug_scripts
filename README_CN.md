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

采集完整保留 ring：

```bash
./06_collect_all_gpu_debug.sh --pid 4008481 --full-ring
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

```bash
python3 02_umr_queue_doctor.py --pid 4008481
python3 02_umr_queue_doctor.py --pid 4008481 --gpu-id 0 1
python3 02_umr_queue_doctor.py \
  --pid 4008481 --queue 28 --samples 3 --interval 10 --full-ring
```

未指定 `--pid` 时，默认通过 `amd-smi` 查找 `torch_server.bin`。

### `03_dump_sdma_registers.sh`

只读采集所有 GPU 和 SDMA instance 的 status、GFX、PAGE、RLC 与 XNACK
寄存器，不写入硬件寄存器。

```bash
./03_dump_sdma_registers.sh
./03_dump_sdma_registers.sh --vmp 1 -o /tmp/sdma-registers.log
```

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
./15_trace_kfd_queue.sh capture ./kfd-queue.log
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

启用后会额外生成 AQL/SDMA gdb dump，保存在 `aql_queue_dump/`（多 PID 时为
`aql_queue_dump_pid<PID>/`）。如需自定义 GDB 或 preload，请在运行 `06` 前
自行 `export GDB=...` 或 `export LD_PRELOAD=...`。

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

Attach 普通 GDB 到 live 进程，通过 `aql_packet_decode_gdb.py` 解码 HSA AQL
queue 和 SDMA blit ring。该脚本只负责 attach/decode/detach，不会启动
workload，也不会修改目标进程内的 ROCm 运行时环境。

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
└── amd-smi.log
```

可选环境变量：

- `GDB`：支持 Python 的 GDB（默认：`/usr/bin/gdb`）
- `GDB_TIMEOUT`：attach/decode 超时秒数（默认：`120`）
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
├── aql_queue_dump/
├── kfd/
├── cpc/
├── waves/
├── sdma-registers.log
├── system-info.log
└── collection-summary.tsv
```

实际目录会根据启用的 collector、目标 PID 和 GPU 数量变化。大部分独立脚本还会
生成 command log、error log、summary 和 `.tgz` 归档。

## 常用环境变量

- `DEBUG_TOOLS_TMP_DIR`：所有工具的默认输出根目录
- `UMR`：UMR 可执行文件或源码目录
- `OUTPUT_DIR`：部分 collector 的输出目录
- `LOG_FILE`：单文件 collector 的输出路径
- `AMD_SMI`：`amd-smi` 可执行文件路径
- `DMESG`：`dmesg` 可执行文件路径
- `GDB`：脚本 `13` 使用的 GDB（须支持 Python；默认：`/usr/bin/gdb`）
- `GDB_TIMEOUT`：脚本 `13` 的 attach/decode 超时（默认：`120`）
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
