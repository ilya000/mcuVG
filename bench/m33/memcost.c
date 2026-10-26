/*
 * mcuVG bench - a QEMU TCG plugin that counts what a Cortex-M33 draw COSTS, not only how many instructions it runs:
 * on cores whose data memory is uncached (the Beken BK7258 application core measured ~10 cycles an access to SRAM or
 * PSRAM, ~4-7 to TCM) loads and stores decide the time and an instruction count alone misleads.
 *
 * The guest marks segments by storing to the mark address: 0xC0DE0000 | id starts segment id, 0xC0DEFFFF ends it and
 * prints its counters.  Inside a segment the plugin counts instructions, the slow ones (UDIV/SDIV, VDIV, VSQRT) and every
 * load and store by address region (regions given as plugin arguments, the rest is "other").
 *
 *   qemu-system-arm ... -plugin file=libmemcost.dylib,mark=0x10fffff0,r=name:lo:hi,r=...
 * Output lines: "seg <id> insn <n> udiv <n> vdiv <n> vsqrt <n> <region> <loads> <stores> ... other <l> <s>"
 */
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <qemu-plugin.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

#define MAXR 8
enum { K_PLAIN = 0, K_UDIV, K_VDIV, K_VSQRT };

static struct { char name[16]; uint64_t lo, hi; } s_reg[MAXR];
static int s_nreg;
static uint64_t s_mark = 0x10FFFFF0u;
static int s_on;
static unsigned s_seg;
static uint64_t s_insn, s_slow[4], s_ld[MAXR + 1], s_st[MAXR + 1];
/* every access inside a segment also counted by the instruction that made it (all segments together): which
   function talks to which memory - the stack's traffic is register spills and calls' saves */
#define PCB 65536u
static uint64_t s_pcbase = 0x10000000u;
static uint32_t s_pc_ld[PCB][MAXR + 1], s_pc_st[PCB][MAXR + 1];

static void report(void)
{
    char line[512];
    int n = snprintf(line, sizeof line, "seg %u insn %" PRIu64 " udiv %" PRIu64 " vdiv %" PRIu64 " vsqrt %" PRIu64,
                     s_seg, s_insn, s_slow[K_UDIV], s_slow[K_VDIV], s_slow[K_VSQRT]);
    for (int r = 0; r <= s_nreg && n < (int)sizeof line - 48; r++)
        n += snprintf(line + n, sizeof line - (size_t)n, " %s %" PRIu64 " %" PRIu64, r < s_nreg ? s_reg[r].name : "other",
                      s_ld[r], s_st[r]);
    snprintf(line + n, sizeof line - (size_t)n, "\n");
    qemu_plugin_outs(line);
}

static void on_insn(unsigned int vcpu, void *ud)
{
    (void)vcpu;
    if (!s_on) return;
    s_insn++;
    s_slow[(uintptr_t)ud]++;
}

static void on_mem(unsigned int vcpu, qemu_plugin_meminfo_t info, uint64_t vaddr, void *ud)
{
    (void)vcpu;
    uint64_t pc = (uint64_t)(uintptr_t)ud;
    int st = qemu_plugin_mem_is_store(info);
    if (st && vaddr == s_mark) {
        qemu_plugin_mem_value v = qemu_plugin_mem_get_value(info);
        uint32_t x = v.data.u32;
        if ((x >> 16) != 0xC0DEu) return;
        if ((x & 0xFFFFu) == 0xFFFFu) {
            if (s_on) report();
            s_on = 0;
        } else {
            s_on = 1;
            s_seg = x & 0xFFFFu;
            s_insn = 0;
            memset(s_slow, 0, sizeof s_slow);
            memset(s_ld, 0, sizeof s_ld);
            memset(s_st, 0, sizeof s_st);
        }
        return;
    }
    if (!s_on) return;
    int r = 0;
    while (r < s_nreg && !(vaddr >= s_reg[r].lo && vaddr < s_reg[r].hi)) r++;
    if (st) s_st[r]++; else s_ld[r]++;
    uint64_t b = (pc - s_pcbase) >> 1;
    if (b < PCB) { if (st) s_pc_st[b][r]++; else s_pc_ld[b][r]++; }
}

static void at_exit(void *ud)
{
    (void)ud;
    char line[256];
    for (uint32_t b = 0; b < PCB; b++) {
        uint64_t any = 0;
        for (int r = 0; r <= s_nreg; r++) any += s_pc_ld[b][r] + s_pc_st[b][r];
        if (!any) continue;
        int n = snprintf(line, sizeof line, "pc %#" PRIx64, s_pcbase + 2u * b);
        for (int r = 0; r <= s_nreg; r++)
            n += snprintf(line + n, sizeof line - (size_t)n, " %u %u", s_pc_ld[b][r], s_pc_st[b][r]);
        snprintf(line + n, sizeof line - (size_t)n, "\n");
        qemu_plugin_outs(line);
    }
}

static int kind_of(const char *d)
{
    while (*d == ' ') d++;
    if (!strncmp(d, "udiv", 4) || !strncmp(d, "sdiv", 4)) return K_UDIV;
    if (!strncmp(d, "vdiv", 4)) return K_VDIV;
    if (!strncmp(d, "vsqrt", 5)) return K_VSQRT;
    return K_PLAIN;
}

static void on_tb(struct qemu_plugin_tb *tb, void *ud)
{
    (void)ud;
    size_t n = qemu_plugin_tb_n_insns(tb);
    for (size_t i = 0; i < n; i++) {
        struct qemu_plugin_insn *insn = qemu_plugin_tb_get_insn(tb, i);
        char *d = qemu_plugin_insn_disas(insn);
        int k = d ? kind_of(d) : K_PLAIN;
        free(d);
        qemu_plugin_register_vcpu_insn_exec_cb(insn, on_insn, QEMU_PLUGIN_CB_NO_REGS, (void *)(uintptr_t)k);
        qemu_plugin_register_vcpu_mem_cb(insn, on_mem, QEMU_PLUGIN_CB_NO_REGS, QEMU_PLUGIN_MEM_RW,
                                         (void *)(uintptr_t)qemu_plugin_insn_vaddr(insn));
    }
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id, const qemu_info_t *info, int argc, char **argv)
{
    (void)info;
    for (int i = 0; i < argc; i++) {
        if (!strncmp(argv[i], "mark=", 5)) {
            s_mark = strtoull(argv[i] + 5, NULL, 0);
        } else if (!strncmp(argv[i], "r=", 2) && s_nreg < MAXR) {
            char name[16];
            unsigned long long lo, hi;
            if (sscanf(argv[i] + 2, "%15[^:]:%lli:%lli", name, &lo, &hi) == 3) {
                snprintf(s_reg[s_nreg].name, sizeof s_reg[s_nreg].name, "%s", name);
                s_reg[s_nreg].lo = lo;
                s_reg[s_nreg].hi = hi;
                s_nreg++;
            }
        }
    }
    qemu_plugin_register_vcpu_tb_trans_cb(id, on_tb, NULL);
    qemu_plugin_register_atexit_cb(id, at_exit, NULL);
    return 0;
}
