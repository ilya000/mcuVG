# mcuVG - host build: the library, the example and the checks.  The library itself is two C files and two headers:
# copy src/ into a firmware project and compile them with the project's flags.
CC ?= cc
CFLAGS ?= -O2 -std=c99 -Wall -Wextra -Wshadow -Wconversion -Wno-sign-conversion
SRC = src/mcuvg.c src/mcuvg_raster.c

all: build/render

build/render: examples/render.c $(SRC) src/mcuvg.h src/mcuvg_raster.h
	@mkdir -p build
	$(CC) $(CFLAGS) -Isrc examples/render.c $(SRC) -o $@

# the C decoder against the reference decoder, cut and damaged pictures, versions 1 and 2, little layer memory, and a
# build without version 2 (needs python3 with numpy, Pillow); both builds warning-free
check: build/render
	$(CC) $(CFLAGS) -Werror -DMCUVG_V2=0 -c src/mcuvg_raster.c -o build/v1_raster.o
	$(CC) $(CFLAGS) -Werror -DMCUVG_V2=0 -c src/mcuvg.c -o build/v1_decoder.o
	python3 tools/mcuvg_check.py tests/svg tests/svg2

# the rasteriser against resvg (needs node and @resvg/resvg-js: cd tools && npm install)
check-resvg:
	python3 tools/vg_check.py --size 112 tests/svg tests/svg2

clean:
	rm -rf build bench/m33/out

.PHONY: all check check-resvg clean
