/* mcuVG bench
 *
 * m33_start.c - bare start for det_probe on QEMU's mps2-an505 (Cortex-M33 with its single-precision FPU): the vector
 * table at the secure alias of SSRAM1 (0x10000000, where the AN505 reads it at reset), the FPU on, .bss zeroed,
 * output and exit through ARM semihosting (bkpt 0xAB). memset / memcpy are the core's only libc calls (arm_size.sh).
 * FPDSCR is left at its reset value - the AP image never writes it (src/base: only saved and restored around nap):
 * round to nearest, denormals kept, no default NaN. DET_FZ=1 sets flush-to-zero instead, to see whether it would matter.
 */
#include <stdint.h>
#include <stddef.h>

int main(void);
extern uint32_t __bss_start, __bss_end, __stack_top;

static int semi(int op, const void *arg)
{
    register int r0 __asm("r0") = op;
    register const void *r1 __asm("r1") = arg;
    __asm volatile ("bkpt 0xAB" : "+r"(r0) : "r"(r1) : "memory");
    return r0;
}
void out_str(const char *s) { semi(0x04, s); }                 /* SYS_WRITE0 */
void finish(int code)
{
    uint32_t a[2] = { 0x20026u, (uint32_t)code };              /* ADP_Stopped_ApplicationExit, the exit code */
    semi(0x20, a);                                            /* SYS_EXIT_EXTENDED */
    semi(0x18, (const void *)0x20026u);                       /* SYS_EXIT, if the extended one is not there */
    for (;;) { }
}

void *memset(void *d, int c, size_t n) { unsigned char *p = d; while (n--) *p++ = (unsigned char)c; return d; }
void *memcpy(void *d, const void *s, size_t n) { unsigned char *p = d; const unsigned char *q = s; while (n--) *p++ = *q++; return d; }
void *memmove(void *d, const void *s, size_t n)
{
    unsigned char *p = d; const unsigned char *q = s;
    if (p < q) while (n--) *p++ = *q++;
    else { p += n; q += n; while (n--) *--p = *--q; }
    return d;
}

static void fault(void) { out_str("FAULT\n"); finish(3); }
/* a probe may sample with SysTick (cost_probe.c COST_PROF): its handler replaces this one */
void systick_handler(void) __attribute__((weak, alias("fault")));

void reset_handler(void)
{
    *(volatile uint32_t *)0xE000ED88u |= 0xFu << 20;          /* CPACR: CP10, CP11 full access */
    __asm volatile ("dsb\n isb" ::: "memory");
#if defined(DET_FZ) && DET_FZ
    uint32_t fpscr;
    __asm volatile ("vmrs %0, fpscr" : "=r"(fpscr));
    fpscr |= 1u << 24;                                        /* FZ */
    __asm volatile ("vmsr fpscr, %0" :: "r"(fpscr));
#endif
    for (uint32_t *p = &__bss_start; p < &__bss_end; ) *p++ = 0;
    finish(main());
}

__attribute__((section(".vectors"), used)) static const void *const s_vectors[16] = {
    &__stack_top, (const void *)reset_handler,
    (const void *)fault, (const void *)fault, (const void *)fault, (const void *)fault, (const void *)fault,
    (const void *)fault, 0, 0, 0, 0, 0, 0, 0, (const void *)systick_handler,
};
