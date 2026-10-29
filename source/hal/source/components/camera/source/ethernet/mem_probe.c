/*
 * SPDX-FileCopyrightText: Copyright 2026 Arm Limited and/or its
 * affiliates <open-source-office@arm.com>
 * SPDX-License-Identifier: Apache-2.0
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#include "mem_probe.h"

#include <inttypes.h>
#include <stdio.h>

/**
 * Set immediately before the risky read (inside the same asm block, so the
 * compiler cannot reorder anything between the two), cleared right after.
 * If a fault lands while this is true, the fault handler knows to recover
 * rather than halt.
 */
static volatile uint32_t g_probe_resume_pc;
static volatile int g_probe_active;
static volatile int g_probe_faulted;

bool mem_probe_read32(uint32_t address, uint32_t* out_value)
{
    uint32_t value = 0;

    g_probe_faulted = 0;
    g_probe_active = 1;

    /* Everything -- computing the resume address, publishing it, and the
     * actual read -- happens in one asm block with explicit clobbers, so
     * there is no C-level statement the compiler could reorder relative to
     * the read. r2/r3 are scratch; "1:" is the resume point the fault
     * handler jumps back to if the read below faults. */
    __asm volatile(
        "adr r2, 1f\n"
        "ldr r3, =g_probe_resume_pc\n"
        "str r2, [r3]\n"
        "ldr %0, [%1]\n"
        "1:\n"
        : "=r"(value)
        : "r"(address)
        : "r2", "r3", "memory");

    g_probe_active = 0;

    if (g_probe_faulted) {
        return false;
    }

    *out_value = value;
    return true;
}

bool mem_probe_write32(uint32_t address, uint32_t value)
{
    g_probe_faulted = 0;
    g_probe_active = 1;

    /* Mirrors mem_probe_read32(): resume address computed and published
     * inside the same asm block as the risky store, so nothing can be
     * reordered between the two. */
    __asm volatile(
        "adr r2, 1f\n"
        "ldr r3, =g_probe_resume_pc\n"
        "str r2, [r3]\n"
        "str %0, [%1]\n"
        "1:\n"
        :
        : "r"(value), "r"(address)
        : "r2", "r3", "memory");

    g_probe_active = 0;

    return !g_probe_faulted;
}

/**
 * @brief   C-level continuation of HardFault_Handler, called with r0 set to
 *          the value MSP had at the moment of the fault -- i.e. a pointer to
 *          the 8-word integer register frame the CPU pushed on exception
 *          entry (r0,r1,r2,r3,r12,LR,PC,xPSR, in that order).
 *
 * If a probe is in progress, this overwrites the stacked PC (index 6) with
 * the resume address recorded by mem_probe_read32() and returns; returning
 * here lets the compiler's own epilogue restore LR (still the EXC_RETURN
 * value the hardware set on entry, untouched by this call chain), whose
 * subsequent load into PC is exactly what triggers the CPU's normal
 * exception-return unstacking -- now reading back the resume address this
 * function just wrote into the frame.
 *
 * If no probe is active, this is an unrelated fault: replicate the same
 * register-dump-and-halt behaviour as the weak default handler this file
 * overrides (see handlers.c), so nothing is lost for faults this module
 * didn't cause.
 */
void mem_probe_hard_fault_c(uint32_t* exception_frame);
void mem_probe_hard_fault_c(uint32_t* exception_frame)
{
    if (g_probe_active) {
        g_probe_faulted = 1;
        exception_frame[6] = g_probe_resume_pc; /* stacked PC -> skip the faulting instruction */
        return;
    }

    printf("\n");
    printf("Exception caught by function HardFault_Handler\n");
    printf("Stacked PC : 0x%08" PRIx32 "\n", exception_frame[6]);
    printf("Stacked LR : 0x%08" PRIx32 "\n", exception_frame[5]);
    printf("Stacked PSR: 0x%08" PRIx32 "\n", exception_frame[7]);
    while (1) {
        __asm volatile("");
    }
}

/**
 * Overrides the weak HardFault_Handler from
 * source/hal/source/components/cmsis_device/source/handlers.c. Naked so
 * nothing runs before "mrs r0, msp" captures the exception frame pointer
 * exactly as the hardware left it, and so the branch below preserves LR
 * (the EXC_RETURN sentinel the hardware set on entry) unmodified for
 * mem_probe_hard_fault_c's own epilogue to eventually return through.
 */
__attribute__((naked)) void HardFault_Handler(void)
{
    __asm volatile(
        "mrs r0, msp\n"
        "b mem_probe_hard_fault_c\n");
}
