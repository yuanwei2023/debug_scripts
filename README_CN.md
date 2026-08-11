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

指定 PID：

```bash
./06_collect_all_gpu_debug.sh --pid 4008481
```

按进程名自动发现 PID：

```bash
./06_collect_all_gpu_debug.sh --process-name torch_server.bin
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

### `05_dump_all_cpc_info.sh`

采集所有 GPU/XCC 的 CPC、Wave、KFD runlist、firmware、module parameter
和 kernel log，并生成 `.tgz`。

```bash
./05_dump_all_cpc_info.sh
```

该脚本会检查并可能启用 `amdgpu.halt_if_hws_hang`。仅应在受控调试环境使用。

### `06_collect_all_gpu_debug.sh`

推荐入口。按顺序运行 Queue Doctor、SDMA、KFD、CPC 和 Wave collector，
最后生成一个统一 debug bundle。

```bash
./06_collect_all_gpu_debug.sh --pid 4008481
./06_collect_all_gpu_debug.sh \
  --pid 4008481 --samples 3 --interval 5 --vmp 0
```

可使用 `--skip-queue`、`--skip-sdma`、`--skip-kfd`、`--skip-cpc` 或
`--skip-waves` 禁用单项采集。

### `07_demo_queue_auto_discovery.sh`

Queue 自动发现测试的兼容入口，转发参数到
`tests/run_queue_auto_discovery_test.sh`。

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

## Debug bundle 结构

典型的一键采集目录：

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

实际目录会根据启用的 collector、目标 PID 和 GPU 数量变化。大部分独立脚本还会
生成 command log、error log、summary 和 `.tgz` 归档。

## 常用环境变量

- `DEBUG_TOOLS_TMP_DIR`：所有工具的默认输出根目录
- `UMR`：UMR 可执行文件或源码目录
- `OUTPUT_DIR`：部分 collector 的输出目录
- `LOG_FILE`：单文件 collector 的输出路径
- `AMD_SMI`：`amd-smi` 可执行文件路径
- `DMESG`：`dmesg` 可执行文件路径

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

## License

详见 [LICENSE](LICENSE)。

## Author

Perry Yuan — <Perry_Yuan@outlook.com>
