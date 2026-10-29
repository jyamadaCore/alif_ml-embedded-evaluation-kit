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

/**
 * Fault-tolerant memory probe: attempts a 32-bit read from an arbitrary
 * address and reports whether it succeeded, instead of letting an unbacked
 * address take down the whole system. Used to scan a list of candidate
 * peripheral base addresses in a single boot, rather than one guess (and one
 * full rebuild/reflash/run cycle) at a time.
 *
 * Overrides the weak HardFault_Handler from
 * source/hal/source/components/cmsis_device/source/handlers.c while doing
 * so; outside of an active probe, it falls back to the same
 * register-dump-and-halt behaviour that default handler provides, so this
 * does not weaken fault reporting for anything unrelated to the scan.
 */
#ifndef MEM_PROBE_H
#define MEM_PROBE_H

#include <stdbool.h>
#include <stdint.h>

#if defined(__cplusplus)
extern "C" {
#endif

/**
 * @brief   Attempts to read one 32-bit word from `address`.
 * @param[in]   address   Address to probe.
 * @param[out]  out_value Set to the read value if the read succeeded; left
 *                        unchanged if it did not.
 * @return  true if the read completed without faulting, false if the CPU
 *          took a HardFault while attempting it (in which case the fault was
 *          caught and recovered from -- execution continues normally either way).
 */
bool mem_probe_read32(uint32_t address, uint32_t* out_value);

/**
 * @brief   Attempts to write one 32-bit word to `address`.
 * @param[in]   address Address to probe.
 * @param[in]   value   Value to write.
 * @return  true if the write completed without faulting, false if the CPU
 *          took a HardFault while attempting it (caught and recovered from
 *          either way).
 */
bool mem_probe_write32(uint32_t address, uint32_t value);

#if defined(__cplusplus)
}
#endif

#endif /* MEM_PROBE_H */
