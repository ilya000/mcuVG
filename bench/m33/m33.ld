/* det_probe on QEMU mps2-an505: everything in SSRAM1's secure alias (4 MB), the vector table first (SVTOR resets to it) */
MEMORY { RAM (rwx) : ORIGIN = 0x10000000, LENGTH = 4M }
ENTRY(reset_handler)
SECTIONS
{
    .vectors : { KEEP(*(.vectors)) } > RAM
    .text : { *(.text*) *(.rodata*) } > RAM
    .ARM.exidx : { *(.ARM.exidx*) } > RAM
    .data : { *(.data*) } > RAM
    .bss (NOLOAD) : ALIGN(4) { __bss_start = .; *(.bss*) *(COMMON) . = ALIGN(4); __bss_end = .; } > RAM
    __stack_top = ORIGIN(RAM) + LENGTH(RAM);
}
