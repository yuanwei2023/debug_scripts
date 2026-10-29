// Controllable GPU workload for exercising the debug collectors.
//
// Keeps KFD user queues alive with resident waves, blocked barrier packets,
// pending dispatches and SDMA traffic until Ctrl-C / SIGTERM / --duration.
// On shutdown a host-coherent release flag lets every spinning wave and every
// stream-wait drain, so the GPU is left clean (no reset needed).
#include <hip/hip_runtime.h>

#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#define HIP_CHECK(expr)                                                        \
    do {                                                                       \
        hipError_t err_ = (expr);                                              \
        if (err_ != hipSuccess) {                                              \
            std::fprintf(stderr, "HIP error %s (%d) at %s:%d: %s\n",           \
                         hipGetErrorString(err_), err_, __FILE__, __LINE__,    \
                         #expr);                                               \
            std::exit(1);                                                      \
        }                                                                      \
    } while (0)

constexpr int kMaxGpus = 64;

// Host-coherent page shared by every GPU.
struct Shared {
    unsigned int release;  // 0 = keep spinning / waiting, 1 = drain
    unsigned int pad;
    unsigned long long heartbeat[kMaxGpus];  // spin_kernel progress per GPU
    unsigned long long noop_done[kMaxGpus];  // pending noops that retired
};

__global__ void spin_kernel(Shared* sh, int slot) {
    unsigned int iter = 0;
    while (__hip_atomic_load(&sh->release, __ATOMIC_RELAXED,
                             __HIP_MEMORY_SCOPE_SYSTEM) == 0) {
        __builtin_amdgcn_s_sleep(127);
        if (blockIdx.x == 0 && threadIdx.x == 0 && (++iter & 0x3ff) == 0)
            __hip_atomic_fetch_add(&sh->heartbeat[slot], 1ull, __ATOMIC_RELAXED,
                                   __HIP_MEMORY_SCOPE_SYSTEM);
    }
}

__global__ void noop_kernel(Shared* sh, int slot) {
    if (blockIdx.x == 0 && threadIdx.x == 0)
        __hip_atomic_fetch_add(&sh->noop_done[slot], 1ull, __ATOMIC_RELAXED,
                               __HIP_MEMORY_SCOPE_SYSTEM);
}

static volatile std::sig_atomic_t g_stop = 0;
static void on_signal(int) { g_stop = 1; }

static std::atomic<bool> g_sdma_stop{false};
static std::atomic<unsigned long long> g_sdma_bytes[kMaxGpus];

struct Options {
    std::vector<int> gpus{0};
    bool all_gpus = false;
    std::vector<std::string> modes{"spin"};
    int duration = 0;  // seconds, 0 = until signal
    int interval = 5;
    int blocks = 0;  // 0 = CU count
    int threads = 256;
    int pending = 8;
    size_t copy_mb = 256;
    std::string pid_file;
};

static void usage(const char* prog) {
    std::printf(
        "Usage: %s [options]\n"
        "\n"
        "  --gpus LIST       HIP device ids, comma separated, or 'all' (default 0)\n"
        "  --mode LIST       comma separated, one stream per mode per GPU (default spin)\n"
        "                      spin  resident waves poll a host flag (hang-like, waves visible)\n"
        "                      wait  hipStreamWaitValue32 on the flag blocks the queue (signal wait)\n"
        "                      sdma  continuous H2D/D2H hipMemcpyAsync (SDMA queue traffic)\n"
        "                      idle  queue created, one noop retired, then idle\n"
        "  --pending N       noop dispatches queued behind spin/wait (default 8)\n"
        "  --blocks N        spin grid size (default: CU count)\n"
        "  --threads N       spin block size (default 256)\n"
        "  --copy-mb N       sdma transfer size in MiB (default 256)\n"
        "  --duration SEC    release and exit after SEC seconds (default 0 = until Ctrl-C)\n"
        "  --interval SEC    status print interval (default 5)\n"
        "  --pid-file PATH   write own PID to PATH\n",
        prog);
}

static std::vector<std::string> split(const std::string& s) {
    std::vector<std::string> out;
    std::stringstream ss(s);
    std::string item;
    while (std::getline(ss, item, ','))
        if (!item.empty()) out.push_back(item);
    return out;
}

static Options parse(int argc, char** argv) {
    Options o;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        auto next = [&]() -> std::string {
            if (i + 1 >= argc) {
                std::fprintf(stderr, "missing value for %s\n", a.c_str());
                std::exit(2);
            }
            return argv[++i];
        };
        if (a == "-h" || a == "--help") {
            usage(argv[0]);
            std::exit(0);
        } else if (a == "--gpus") {
            std::string v = next();
            o.gpus.clear();
            if (v == "all") o.all_gpus = true;
            else for (auto& g : split(v)) o.gpus.push_back(std::atoi(g.c_str()));
        } else if (a == "--mode") {
            o.modes = split(next());
        } else if (a == "--pending") {
            o.pending = std::atoi(next().c_str());
        } else if (a == "--blocks") {
            o.blocks = std::atoi(next().c_str());
        } else if (a == "--threads") {
            o.threads = std::atoi(next().c_str());
        } else if (a == "--copy-mb") {
            o.copy_mb = std::strtoull(next().c_str(), nullptr, 10);
        } else if (a == "--duration") {
            o.duration = std::atoi(next().c_str());
        } else if (a == "--interval") {
            o.interval = std::max(1, std::atoi(next().c_str()));
        } else if (a == "--pid-file") {
            o.pid_file = next();
        } else {
            std::fprintf(stderr, "unknown option: %s\n", a.c_str());
            usage(argv[0]);
            std::exit(2);
        }
    }
    for (auto& m : o.modes) {
        if (m != "spin" && m != "wait" && m != "sdma" && m != "idle") {
            std::fprintf(stderr, "unknown mode: %s\n", m.c_str());
            std::exit(2);
        }
    }
    return o;
}

static void sdma_loop(int dev, size_t bytes) {
    HIP_CHECK(hipSetDevice(dev));
    hipStream_t s;
    HIP_CHECK(hipStreamCreateWithFlags(&s, hipStreamNonBlocking));
    void* h = nullptr;
    void* d = nullptr;
    HIP_CHECK(hipHostMalloc(&h, bytes, hipHostMallocDefault));
    HIP_CHECK(hipMalloc(&d, bytes));
    std::memset(h, 0x5a, bytes);
    while (!g_sdma_stop.load()) {
        HIP_CHECK(hipMemcpyAsync(d, h, bytes, hipMemcpyHostToDevice, s));
        HIP_CHECK(hipMemcpyAsync(h, d, bytes, hipMemcpyDeviceToHost, s));
        HIP_CHECK(hipStreamSynchronize(s));
        g_sdma_bytes[dev] += 2 * bytes;
    }
    HIP_CHECK(hipFree(d));
    HIP_CHECK(hipHostFree(h));
    HIP_CHECK(hipStreamDestroy(s));
}

static bool has(const std::vector<std::string>& v, const char* m) {
    for (auto& x : v)
        if (x == m) return true;
    return false;
}

int main(int argc, char** argv) {
    Options opt = parse(argc, argv);
    std::signal(SIGINT, on_signal);
    std::signal(SIGTERM, on_signal);
    std::setvbuf(stdout, nullptr, _IOLBF, 0);

    int ndev = 0;
    HIP_CHECK(hipGetDeviceCount(&ndev));
    if (opt.all_gpus)
        for (int i = 0; i < ndev; ++i) opt.gpus.push_back(i);
    for (int g : opt.gpus) {
        if (g < 0 || g >= ndev || g >= kMaxGpus) {
            std::fprintf(stderr, "invalid GPU %d (found %d devices)\n", g, ndev);
            return 2;
        }
    }

    if (!opt.pid_file.empty()) {
        FILE* f = std::fopen(opt.pid_file.c_str(), "w");
        if (!f) {
            std::perror(opt.pid_file.c_str());
            return 1;
        }
        std::fprintf(f, "%d\n", getpid());
        std::fclose(f);
    }

    Shared* sh = nullptr;
    HIP_CHECK(hipSetDevice(opt.gpus.front()));
    HIP_CHECK(hipHostMalloc(reinterpret_cast<void**>(&sh), sizeof(Shared),
                            hipHostMallocCoherent | hipHostMallocMapped |
                                hipHostMallocPortable));
    std::memset(sh, 0, sizeof(Shared));

    std::printf("PID           : %d\n", getpid());
    std::printf("Modes         : ");
    for (size_t i = 0; i < opt.modes.size(); ++i)
        std::printf("%s%s", i ? "," : "", opt.modes[i].c_str());
    std::printf("\nRelease flag  : %p (host-coherent)\n", (void*)&sh->release);

    std::vector<hipStream_t> streams;
    std::vector<std::thread> sdma_threads;

    for (int g : opt.gpus) {
        HIP_CHECK(hipSetDevice(g));
        hipDeviceProp_t prop;
        HIP_CHECK(hipGetDeviceProperties(&prop, g));
        char bdf[32] = {};
        HIP_CHECK(hipDeviceGetPCIBusId(bdf, sizeof(bdf), g));
        int blocks = opt.blocks > 0 ? opt.blocks : prop.multiProcessorCount;
        std::printf("GPU %-2d        : %s %s BDF %s CUs %d\n", g, prop.name,
                    prop.gcnArchName, bdf, prop.multiProcessorCount);

        for (auto& mode : opt.modes) {
            if (mode == "sdma") {
                sdma_threads.emplace_back(sdma_loop, g, opt.copy_mb << 20);
                std::printf("  sdma        : %zu MiB H2D+D2H loop\n", opt.copy_mb);
                continue;
            }
            hipStream_t s;
            HIP_CHECK(hipStreamCreateWithFlags(&s, hipStreamNonBlocking));
            streams.push_back(s);
            if (mode == "spin") {
                spin_kernel<<<blocks, opt.threads, 0, s>>>(sh, g);
                HIP_CHECK(hipGetLastError());
                std::printf("  spin        : grid %d x %d, %d noops pending behind\n",
                            blocks, opt.threads, opt.pending);
            } else if (mode == "wait") {
                int can_wait = 0;
                HIP_CHECK(hipDeviceGetAttribute(
                    &can_wait, hipDeviceAttributeCanUseStreamWaitValue, g));
                if (!can_wait) {
                    std::fprintf(stderr, "GPU %d does not support hipStreamWaitValue32\n", g);
                    return 1;
                }
                HIP_CHECK(hipStreamWaitValue32(s, &sh->release, 1,
                                               hipStreamWaitValueGte, 0xffffffffu));
                std::printf("  wait        : queue blocked on release flag >= 1, "
                            "%d noops pending behind\n", opt.pending);
            } else {  // idle
                noop_kernel<<<1, 64, 0, s>>>(sh, g);
                HIP_CHECK(hipGetLastError());
                HIP_CHECK(hipStreamSynchronize(s));
                std::printf("  idle        : queue created, idle\n");
                continue;
            }
            for (int i = 0; i < opt.pending; ++i) noop_kernel<<<1, 64, 0, s>>>(sh, g);
            HIP_CHECK(hipGetLastError());
        }
    }

    std::printf("\nCollect, e.g.: ./06_collect_all_gpu_debug.sh --pid %d\n", getpid());
    std::printf("Stop with Ctrl-C or: kill %d%s\n\n", getpid(),
                opt.duration ? (" (auto release after " + std::to_string(opt.duration) + "s)").c_str() : "");

    auto t0 = std::chrono::steady_clock::now();
    auto last = t0;
    while (!g_stop) {
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
        auto now = std::chrono::steady_clock::now();
        double elapsed = std::chrono::duration<double>(now - t0).count();
        if (opt.duration > 0 && elapsed >= opt.duration) break;
        if (std::chrono::duration<double>(now - last).count() < opt.interval) continue;
        last = now;
        std::printf("[%7.1fs]", elapsed);
        for (int g : opt.gpus) {
            std::printf(" gpu%d hb=%llu noop=%llu", g,
                        __atomic_load_n(&sh->heartbeat[g], __ATOMIC_RELAXED),
                        __atomic_load_n(&sh->noop_done[g], __ATOMIC_RELAXED));
            if (has(opt.modes, "sdma"))
                std::printf(" sdma=%.1fGiB", g_sdma_bytes[g].load() / double(1ull << 30));
        }
        std::printf("\n");
    }

    std::printf("\nReleasing GPU work...\n");
    __atomic_store_n(&sh->release, 1u, __ATOMIC_SEQ_CST);
    g_sdma_stop = true;
    for (auto& t : sdma_threads) t.join();

    // Halted waves (e.g. by a wave dump) never drain; don't hang forever.
    std::atomic<bool> drained{false};
    std::thread syncer([&] {
        for (int g : opt.gpus) {
            HIP_CHECK(hipSetDevice(g));
            HIP_CHECK(hipDeviceSynchronize());
        }
        drained = true;
    });
    for (int i = 0; i < 300 && !drained; ++i)
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    if (!drained) {
        std::fprintf(stderr, "GPU work did not drain in 30s (waves halted?); exiting anyway\n");
        std::_Exit(3);
    }
    syncer.join();

    for (int g : opt.gpus)
        std::printf("gpu%d: heartbeat=%llu noop_done=%llu\n", g, sh->heartbeat[g],
                    sh->noop_done[g]);
    for (auto s : streams) HIP_CHECK(hipStreamDestroy(s));
    HIP_CHECK(hipHostFree(sh));
    std::printf("Done.\n");
    return 0;
}
