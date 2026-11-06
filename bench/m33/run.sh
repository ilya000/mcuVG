#!/bin/bash
# mcuVG bench - the rasteriser and the decoder on QEMU's Cortex-M33 (mps2-an505): builds vg_bench.c at -Os and -O2, prints
# instructions per picture and checks the pixels against the host's (the same C built for the host by tools/vg_host.py).
#   bench/m33/run.sh                 instructions and checksums
#   bench/m33/run.sh prof            a sampling profile by function (-Os, inlining off so helpers show up separately)
#   bench/m33/run.sh cost [Os|O2]    loads and stores by memory region (the memcost QEMU plugin) and modelled cycles
# Needs arm-none-eabi-gcc (PATH or ARM_GCC_BIN), qemu-system-arm with plugins, python3 with numpy and Pillow.
set -eu
D="$(cd "$(dirname "$0")" && pwd)"
V="$D/../../src"
TC="${ARM_GCC_BIN:-$(dirname "$(command -v arm-none-eabi-gcc)")}"
mkdir -p "$D/out"
python3 "$D/gen_icons.py"
AF="-mcpu=cortex-m33 -mthumb -mfpu=fpv5-sp-d16 -mfloat-abi=hard -std=c99 -ffunction-sections -fdata-sections -Wall -Wextra
    -ffreestanding -fno-tree-loop-distribute-patterns -fno-common -ffp-contract=off -fno-math-errno -I$V -I$D ${MCUVG_CFLAGS:-}"
# MCUVG_CFLAGS=-DMCUVG_V2=0: the version 1 build a device without version 2 uses
if [ "${1:-}" = cost ]; then
    # the plugin: qemu-plugin.h and glib's headers (Homebrew paths by default; set QEMU_INC / GLIB_CFLAGS elsewhere)
    QI="${QEMU_INC:-$(brew --prefix qemu 2>/dev/null)/include}"
    GC="${GLIB_CFLAGS:-$(pkg-config --cflags glib-2.0 2>/dev/null)}"
    cc -O2 -shared -fPIC -undefined dynamic_lookup -I"$QI" $GC -o "$D/out/libmemcost.dylib" "$D/memcost.c"
    "$TC/arm-none-eabi-gcc" $AF -${2:-Os} -DVG_COST "$D/vg_bench.c" "$V/mcuvg_raster.c" "$V/mcuvg.c" "$D/m33_start.c" -nostartfiles \
        -nostdlib -Wl,--gc-sections -Wl,--no-warn-rwx-segments -T "$D/m33.ld" -lgcc -o "$D/out/vg_cost.elf"
    R=$("$TC/arm-none-eabi-nm" -S "$D/out/vg_cost.elf" | python3 -c '
import sys
s = {}
for l in sys.stdin:
    p = l.split()
    if len(p) == 4: s[p[3]] = (int(p[0], 16), int(p[1], 16))
    elif len(p) == 3: s[p[2]] = (int(p[0], 16), 0)
def r(n, k): a, z = s[k]; return f"r={n}:{a:#x}:{a + z:#x}"
pics = [v for k, v in s.items() if k.startswith("pic_")]
lo = min(a for a, z in pics); hi = max(a + z for a, z in pics)
top = s["__stack_top"][0]
m = s["vg_mark"][0]
print(f"mark={m:#x}," + ",".join([r("px", "s_px"), r("mem", "s_mem"), r("ctx", "s_c"), f"r=stack:{top - 0x10000:#x}:{top:#x}", f"r=pics:{lo:#x}:{hi:#x}"]))')
    qemu-system-arm -M mps2-an505 -cpu cortex-m33 -nographic -monitor none -serial none \
        -semihosting-config enable=on,target=native -d plugin -plugin "$D/out/libmemcost.dylib,$R" \
        -kernel "$D/out/vg_cost.elf" > "$D/out/vg_cost.txt" 2>&1
    python3 "$D/cost.py" "$D/out/vg_cost.txt" "$D/out/icons.h"
    python3 "$D/bypc.py" "$TC/arm-none-eabi-nm" "$D/out/vg_cost.elf" "$D/out/vg_cost.txt"
    exit 0
fi
if [ "${1:-}" = prof ]; then
    "$TC/arm-none-eabi-gcc" $AF -Os -DVG_PROF -fno-inline -fno-inline-small-functions "$D/vg_bench.c" "$V/mcuvg_raster.c" \
        "$V/mcuvg.c" "$D/m33_start.c" -nostartfiles -nostdlib -Wl,--gc-sections -Wl,--no-warn-rwx-segments \
        -T "$D/m33.ld" -lgcc -o "$D/out/vg_prof.elf"
    perl -e 'alarm shift; exec @ARGV' 1800 qemu-system-arm -M mps2-an505 -cpu cortex-m33 -icount shift=0 -nographic \
        -monitor none -serial none -semihosting-config enable=on,target=native -kernel "$D/out/vg_prof.elf" > "$D/out/vg_prof.txt" 2>&1
    python3 "$D/prof.py" "$TC/arm-none-eabi-nm" "$D/out/vg_prof.elf" "$D/out/vg_prof.txt"
    exit 0
fi
worst=0
for o in Os O2; do
    "$TC/arm-none-eabi-gcc" $AF -$o "$D/vg_bench.c" "$V/mcuvg_raster.c" "$V/mcuvg.c" "$D/m33_start.c" -nostartfiles -nostdlib \
        -Wl,--gc-sections -Wl,--no-warn-rwx-segments -T "$D/m33.ld" -lgcc -o "$D/out/vg_bench_$o.elf"
    "$TC/arm-none-eabi-gcc" $AF -$o -c "$V/mcuvg_raster.c" -o "$D/out/mcuvg_raster_$o.o"
    "$TC/arm-none-eabi-gcc" $AF -$o -c "$V/mcuvg.c" -o "$D/out/mcuvg_$o.o"
    echo "== -$o: code $("$TC/arm-none-eabi-size" "$D/out/mcuvg_raster_$o.o" | awk 'NR==2{print $1}') B rasteriser + $("$TC/arm-none-eabi-size" "$D/out/mcuvg_$o.o" | awk 'NR==2{print $1}') B decoder"
    rc=0
    perl -e 'alarm shift; exec @ARGV' 1800 qemu-system-arm -M mps2-an505 -cpu cortex-m33 -icount shift=0 -nographic \
        -monitor none -serial none -semihosting-config enable=on,target=native -kernel "$D/out/vg_bench_$o.elf" 2>&1 || rc=$?
    echo "   (exit $rc)"
    worst=$(( rc > worst ? rc : worst ))
done
exit $worst
