/*
 * mcuVG bench - the rasteriser on a Cortex-M33 (QEMU mps2-an505, -icount shift=0): instructions to draw each
 * bench picture at 112, 240 and 480 px into RGB565, the same picture turned (an object drawn every frame), and the
 * checksum of the pixels against the host's (gen_icons.py -> expect.h): the M33 must draw exactly what the host draws.
 * Instructions, not time on hardware: see cost.py for the memory model.
 */
#include <stdint.h>
#include <stddef.h>
#include "mcuvg.h"
#include "out/expect.h"
#include "out/icons.h"

void out_str(const char *s);
#define SYST_CSR (*(volatile uint32_t *)0xE000E010u)
#define SYST_RVR (*(volatile uint32_t *)0xE000E014u)
#define SYST_CVR (*(volatile uint32_t *)0xE000E018u)
static uint32_t span(uint32_t a, uint32_t b) { return (a - b) & 0xFFFFFFu; }

static void out_dec(uint32_t v)
{
    char b[12];
    int i = 11;
    b[i] = 0;
    do { b[--i] = (char)('0' + v % 10u); v /= 10u; } while (v);
    out_str(b + i);
}
static void out_hex(uint32_t v)
{
    char b[9];
    for (int i = 7; i >= 0; i--) { b[i] = "0123456789ABCDEF"[v & 15u]; v >>= 4; }
    b[8] = 0;
    out_str(b);
}

__attribute__((noinline)) static void run_nops(void)
{
#define N8 "nop\n nop\n nop\n nop\n nop\n nop\n nop\n nop\n"
#define N64 N8 N8 N8 N8 N8 N8 N8 N8
#define N512 N64 N64 N64 N64 N64 N64 N64 N64
    __asm volatile (N512 N512 N512 N512 N512 N512 N512 N512 ::: "memory");
}

#ifdef VG_PROF
/* -DVG_PROF: a sampling profile instead of the numbers - SysTick every 100 ticks (~5 000 instructions), the stacked PC
   counted per Thumb halfword over the first 128 KB; prof.py folds the buckets into functions (from the InstaView probe) */
#define BUCKETS 65536u
static uint32_t s_hist[BUCKETS];
static volatile uint32_t s_on;
void prof_sample(const uint32_t *frame)
{
    if (!s_on) return;
    uint32_t b = (frame[6] - 0x10000000u) >> 1;
    if (b < BUCKETS) s_hist[b]++;
}
__attribute__((naked)) void systick_handler(void)
{
    __asm volatile ("tst lr, #4\n ite eq\n mrseq r0, msp\n mrsne r0, psp\n b prof_sample\n");
}
#endif

static uint16_t s_px[480 * 480];
static uint8_t s_mem[160 * 1024] __attribute__((aligned(8)));
static mcuvg_ctx s_c;
static uint32_t s_ipt_x1000;

static uint32_t fnv(const void *p, uint32_t n)
{
    const uint8_t *b = p;
    uint32_t h = 0x811C9DC5u;
    while (n--) h = (h ^ *b++) * 0x01000193u;
    return h;
}

/* one draw: instructions, and the checksum of the target */
static uint32_t draw(const struct pic *p, int size, float deg, uint32_t *sum)
{
    for (int i = 0; i < size * size; i++) s_px[i] = 0;
    mcuvg_init(&s_c, s_px, size, size, size * 2, MCUVG_RGB565, s_mem, sizeof s_mem);
    uint32_t a = SYST_CVR;
    int rc;
    if (deg == 0.0f) {
        float k = (float)size / p->w;
        mcuvg_scale(&s_c, k, k);
        rc = mcuvg_draw(&s_c, p->b, p->len, 0, 0);
    } else {
        rc = mcuvg_fit(&s_c, p->b, p->len, 0, 0, (float)size, (float)size, deg);
    }
    uint32_t b = SYST_CVR;
    *sum = fnv(s_px, (uint32_t)(size * size * 2));
    if (rc) { out_str("  draw failed "); out_dec((uint32_t)-rc); out_str("\n"); }
    return (uint32_t)((uint64_t)span(a, b) * s_ipt_x1000 / 1000u);
}

#ifdef VG_COST
/* -DVG_COST: the draws only, each between marks the memcost plugin reads (segment id = picture * 16 + size, + 8 turned) */
volatile uint32_t vg_mark;                         /* the plugin is told its address (run.sh cost) */
#define MARK vg_mark
static void cost_run(void)
{
    static const int sizes[3] = { 112, 240, 480 };
    for (unsigned i = 0; i < sizeof s_pics / sizeof s_pics[0]; i++)
        for (int s = 0; s < 4; s++) {
            int size = s < 3 ? sizes[s] : 112;
            for (int k = 0; k < size * size; k++) s_px[k] = 0;
            mcuvg_init(&s_c, s_px, size, size, size * 2, MCUVG_RGB565, s_mem, sizeof s_mem);
            MARK = 0xC0DE0000u | (i * 16u + (s < 3 ? (unsigned)s : 8u));
            if (s < 3) {
                float kk = (float)size / s_pics[i].w;
                mcuvg_scale(&s_c, kk, kk);
                (void)mcuvg_draw(&s_c, s_pics[i].b, s_pics[i].len, 0, 0);
            } else {
                (void)mcuvg_fit(&s_c, s_pics[i].b, s_pics[i].len, 0, 0, 112.0f, 112.0f, 37.0f);
            }
            MARK = 0xC0DEFFFFu;
        }
}
#endif

int main(void)
{
#ifdef VG_COST
    cost_run();
    return 0;
#endif
#ifdef VG_PROF
    static const int psz[2] = { 112, 240 };
    SYST_RVR = 99u; SYST_CVR = 0; SYST_CSR = 7u;
    for (int rep = 0; rep < 4; rep++)
        for (unsigned i = 0; i < sizeof s_pics / sizeof s_pics[0]; i++)
            for (int s = 0; s < 2; s++) {
                uint32_t sum;
                for (int k = 0; k < psz[s] * psz[s]; k++) s_px[k] = 0;
                mcuvg_init(&s_c, s_px, psz[s], psz[s], psz[s] * 2, MCUVG_RGB565, s_mem, sizeof s_mem);
                float kk = (float)psz[s] / s_pics[i].w;
                mcuvg_scale(&s_c, kk, kk);
                s_on = 1;
                (void)mcuvg_draw(&s_c, s_pics[i].b, s_pics[i].len, 0, 0);
                s_on = 0;
                (void)sum;
            }
    SYST_CSR = 0;
    for (uint32_t b = 0; b < BUCKETS; b++)
        if (s_hist[b]) { out_hex(0x10000000u + 2u * b); out_str(" "); out_dec(s_hist[b]); out_str("\n"); }
    return 0;
#endif
    SYST_RVR = 0xFFFFFFu; SYST_CVR = 0; SYST_CSR = 5u;
    uint32_t a = SYST_CVR;
    for (int i = 0; i < 256; i++) run_nops();
    uint32_t b = SYST_CVR, tk = span(a, b);
    s_ipt_x1000 = tk ? (uint32_t)(256ull * 4096u * 1000u / tk) : 0;
    out_str("vg_bench: instructions per SysTick tick x1000: "); out_dec(s_ipt_x1000); out_str("\n");
    static const int sizes[3] = { 112, 240, 480 };
    int bad = 0;
    for (unsigned i = 0; i < sizeof s_pics / sizeof s_pics[0]; i++) {
        const struct pic *p = &s_pics[i];
        out_str(p->name); out_str(" ("); out_dec(p->len); out_str(" B):");
        for (int s = 0; s < 3; s++) {
            uint32_t sum, ins = draw(p, sizes[s], 0.0f, &sum);
            out_str("  "); out_dec((uint32_t)sizes[s]); out_str(" px "); out_dec(ins / 1000u); out_str(" k");
            if (sum != p->expect[s]) { out_str(" [checksum "); out_hex(sum); out_str(" != host "); out_hex(p->expect[s]); out_str("]"); bad++; }
        }
        uint32_t sum, ins = draw(p, 112, 37.0f, &sum);
        out_str("  | turned 37 deg at 112 px "); out_dec(ins / 1000u); out_str(" k\n");
        (void)sum;
    }
    out_str(bad ? "CHECKSUMS DIFFER from the host\n" : "all checksums equal to the host's\n");
    return bad ? 1 : 0;
}
