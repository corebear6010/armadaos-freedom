/* SPDX-License-Identifier: GPL-3.0-only */
.section .text.arm64_storage_ufs_store32, "ax"
.balign 4
.global arm64_storage_ufs_store32
.type arm64_storage_ufs_store32, %function
arm64_storage_ufs_store32:
    dsb sy
.global arm64_storage_ufs_write_instruction
arm64_storage_ufs_write_instruction:
    str w1, [x0]
    dsb sy
    mov w0, #0
    ret
.global arm64_storage_ufs_write_failed
arm64_storage_ufs_write_failed:
    mov w0, #-1
    ret
.size arm64_storage_ufs_store32, . - arm64_storage_ufs_store32
.section .note.GNU-stack, "", %progbits
