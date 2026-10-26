#!/usr/bin/env python3
"""mcuVG bench - cycles on an uncached Cortex-M33 core from the memcost plugin's counters.

    cost.py <plugin output> <icons.h>

The model is the Beken BK7258's application core (480 MHz Cortex-M33), which maps all data memory uncached, so a
load or a store costs what the memory costs; latencies measured on that chip, cycles per access:
    TCM read 5.4 write 7.6 | SRAM read 9.8 write 7.6 | PSRAM, a small hot set: read 9.7 write 7.6
    PSRAM, a large set read in order (a raster row): 15.9 | XIP flash data: ~2400 (5 us, wc_gfx.c)
An instruction is 1 cycle (code assumed in the I-cache), UDIV/SDIV +6, VDIV and VSQRT +13 (M33 TRM ranges, middle).
A model, not a measurement: the order of magnitude and the comparison of variants, to be checked on hardware.
"""
import re
import sys

LAT = {  # (read, write) cycles per access
    "TCM": (5.4, 7.6), "SRAM": (9.8, 7.6), "PSRAM": (9.7, 7.6), "PSRAM_SEQ": (15.9, 7.6), "XIP": (2400.0, 7.6),
}
# where each region lives in each scenario
SCEN = {
    "now": {"px": "PSRAM_SEQ", "mem": "PSRAM", "ctx": "SRAM", "stack": "SRAM", "pics": "PSRAM", "other": "SRAM"},
    "pics_in_xip": {"px": "PSRAM_SEQ", "mem": "PSRAM", "ctx": "SRAM", "stack": "SRAM", "pics": "XIP", "other": "SRAM"},
    "tcm_work": {"px": "PSRAM_SEQ", "mem": "TCM", "ctx": "TCM", "stack": "TCM", "pics": "PSRAM", "other": "SRAM"},
}
EXTRA = {"udiv": 6, "vdiv": 13, "vsqrt": 13}
SIZES = {0: "112", 1: "240", 2: "480", 8: "112 turned"}


def main():
    out, icons = sys.argv[1], sys.argv[2]
    names = re.findall(r'\{ "(\w+)", pic_', open(icons).read())
    rows = []
    for line in open(out):
        if not line.startswith("seg "):
            continue
        t = line.split()
        seg = int(t[1])
        d = {t[i]: int(t[i + 1]) for i in range(2, 10, 2)}
        reg = {}
        i = 10
        while i + 2 < len(t):
            reg[t[i]] = (int(t[i + 1]), int(t[i + 2]))
            i += 3
        rows.append((seg, d, reg))
    print("cycles on the AP (model), thousands; ms at 480 MHz for 'now'")
    print(f"{'picture':10s} {'size':11s} {'insn':>7s} {'loads':>7s} {'stores':>7s} {'now':>8s} {'ms':>6s} "
          f"{'xip data':>9s} {'tcm work':>9s}   where the 'now' cycles go")
    for seg, d, reg in rows:
        name = names[seg // 16] if seg // 16 < len(names) else str(seg // 16)
        size = SIZES.get(seg % 16, str(seg % 16))
        ld = sum(v[0] for v in reg.values())
        stv = sum(v[1] for v in reg.values())
        res = {}
        parts = {}
        for sc, place in SCEN.items():
            c = d["insn"] + sum(EXTRA[k] * d[k] for k in EXTRA)
            for r, (l, s) in reg.items():
                rl, sl = LAT[place.get(r, "SRAM")]
                c += l * (rl - 1) + s * (sl - 1)
                if sc == "now":
                    parts[r] = l * (rl - 1) + s * (sl - 1)
            res[sc] = c
        base = d["insn"] + sum(EXTRA[k] * d[k] for k in EXTRA)
        top = sorted(parts.items(), key=lambda kv: -kv[1])[:3]
        where = f"insn {100 * base / res['now']:.0f} %, " + ", ".join(f"{r} {100 * v / res['now']:.0f} %" for r, v in top)
        print(f"{name:10s} {size:11s} {d['insn'] / 1e3:7.0f} {ld / 1e3:7.0f} {stv / 1e3:7.0f} {res['now'] / 1e3:8.0f} "
              f"{res['now'] / 480e3:6.2f} {res['pics_in_xip'] / 1e3:9.0f} {res['tcm_work'] / 1e3:9.0f}   {where}")


if __name__ == "__main__":
    main()
