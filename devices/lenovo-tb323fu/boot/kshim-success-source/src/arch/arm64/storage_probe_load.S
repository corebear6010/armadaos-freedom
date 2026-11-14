/* SPDX-License-Identifier: GPL-3.0-only */
.section .text.arm64_storage_probe_load32, "ax"
.balign 4
.global arm64_storage_probe_load32
.type arm64_storage_probe_load32, %function
arm64_storage_probe_load32:
    dsb sy
.global arm64_storage_probe_read_instruction
arm64_storage_probe_read_instruction:
    ldr w2, [x0]
    dsb sy
    str w2, [x1]
    mov w0, #0
    ret
.global arm64_storage_probe_read_failed
arm64_storage_probe_read_failed:
    mov w0, #-1
    ret
.size arm64_storage_probe_load32, . - arm64_storage_probe_load32
.section .note.GNU-stack, "", %progbits
