/*
 * mcuVG example - draw an .mcuvg file into a PPM image on the host, no dependencies:
 *   cc -O2 -std=c99 -Isrc examples/render.c src/mcuvg.c src/mcuvg_raster.c -o render
 *   ./render picture.mcuvg 112 out.ppm
 * The same three calls run on a microcontroller: mcuvg_init over a raster and one memory block, then mcuvg_fit.
 */
#include <stdio.h>
#include <stdlib.h>
#include "mcuvg.h"

int main(int argc, char **argv)
{
    if (argc != 4) { fprintf(stderr, "usage: render IN.mcuvg SIZE OUT.ppm\n"); return 2; }
    FILE *f = fopen(argv[1], "rb");
    if (!f) { perror(argv[1]); return 1; }
    static unsigned char data[1 << 20];
    size_t len = fread(data, 1, sizeof data, f);
    fclose(f);
    int size = atoi(argv[2]);
    if (size < 1 || size > 4096) { fprintf(stderr, "size 1..4096\n"); return 2; }
    unsigned char *px = calloc((size_t)size * (size_t)size, 4);           /* RGBA8888, premultiplied, transparent */
    size_t need = mcuvg_mem(size, 4096u, 16384u);                         /* generous: the converter prints the real need */
    void *mem = malloc(need);                                             /* on a device: a static block instead */
    mcuvg_ctx c;
    if (!px || !mem || mcuvg_init(&c, px, size, size, size * 4, MCUVG_RGBA8888, mem, need)) return 1;
#if MCUVG_V2
    size_t lneed = (size_t)size * (size_t)size * 6u + 65536u;            /* version 2's layers: generous here too */
    void *layers = malloc(lneed);
    if (layers) mcuvg_layers(&c, layers, lneed);
#endif
    int rc = mcuvg_fit(&c, data, (uint32_t)len, 0, 0, (float)size, (float)size, 0.0f);
    if (rc) { fprintf(stderr, "mcuvg: error %d\n", rc); return 1; }
    FILE *o = fopen(argv[3], "wb");
    if (!o) { perror(argv[3]); return 1; }
    fprintf(o, "P6\n%d %d\n255\n", size, size);
    for (int i = 0; i < size * size; i++) {                               /* over white */
        unsigned a = px[4 * i + 3];
        for (int k = 0; k < 3; k++) fputc((int)(px[4 * i + k] + (255u - a)), o);
    }
    fclose(o);
    free(px);
    free(mem);
#if MCUVG_V2
    free(layers);
#endif
    return 0;
}
