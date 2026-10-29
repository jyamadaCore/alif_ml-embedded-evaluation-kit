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
 * Camera HAL backend for this project's Corellium Atlas configuration, where
 * the video feed arrives over a custom vMMIO entry (Devices -> Add vMMIO
 * entry in the Atlas instance UI) rather than through a VSI peripheral or a
 * guest-visible Ethernet MAC.
 *
 * Per Corellium's CoreIO/ExtIO protocol (see
 * https://support.avh.corellium.com/device-settings/vmmio and the `coreio`
 * package at https://support.avh.corellium.com/files/coreio.tar.gz), a vMMIO
 * entry's address range is a trapped MMIO region serviced one CPU access at a
 * time via a registered read/write handler -- it is not memory an external
 * peer can just blit bytes into. Bulk frame data instead arrives via a
 * `coreio_dma_write()` straight into real guest DRAM at DYNAMIC_IFM_BASE (see
 * scripts/ethernet_camera/video_sender.py), and the vMMIO entry's only
 * remaining job is to carry an IRQ line: the external peer calls
 * `coreio_irq_update()` (1 then 0, an edge) once the DMA write lands, which is
 * what actually fires VMMIO_CAMERA_IRQ on the guest. hal_camera_get_captured_frame()
 * waits on that IRQ, then reads DYNAMIC_IFM_BASE directly -- no copy from
 * VMMIO_CAMERA_BASE needed, since nothing is ever written there.
 */
#include "hal_camera.h"
#include "hal_log.h"
#include "ethernet/mem_probe.h"
#include "peripheral_memmap.h"
#include "peripheral_irqs.h"
#include "RTE_Components.h"

#include <inttypes.h>
#include <string.h>

#ifndef VMMIO_CAMERA_BASE
#error "VMMIO_CAMERA_BASE must be defined (Start address of the Atlas vMMIO entry)"
#endif
#ifndef VMMIO_CAMERA_SIZE
#error "VMMIO_CAMERA_SIZE must be defined (Size of the Atlas vMMIO entry)"
#endif
#ifndef VMMIO_CAMERA_IRQ
#error "VMMIO_CAMERA_IRQ must be defined (IRQ number of the Atlas vMMIO entry)"
#endif

typedef struct hal_camera_device_ {
    char name[32];
    uint32_t frame_width;
    uint32_t frame_height;
    uint32_t bytes_per_frame;
    hal_cam_clr_format format;
    hal_cam_mode mode;
    hal_cam_status status;
    uint8_t* buffer;
    uint32_t buffer_size;
} hal_cam_dev;

static hal_cam_dev s_eth_dev;

/**< Set once the capture buffer has been observed to hold real frame data,
 *   so hal_camera_get_captured_frame() only waits on the very first call. */
static bool s_frame_seen;

/**< Set by VmmioCamera_Handler() when the vMMIO entry's IRQ fires (i.e. an
 *   external write landed in [VMMIO_CAMERA_BASE, VMMIO_CAMERA_BASE +
 *   VMMIO_CAMERA_SIZE)); cleared once that frame has been consumed. */
static volatile bool s_vmmio_frame_pending;

/**< Counts every IRQ firing, independent of s_vmmio_frame_pending, so the
 *   console can distinguish "IRQ never fires" from "IRQ fires but the wait
 *   loop never sees the flag" while debugging a stuck vMMIO camera feed. */
static volatile uint32_t s_vmmio_irq_count;

static void VmmioCamera_Handler(void)
{
    s_vmmio_irq_count++;
    info("vMMIO camera IRQ fired (count=%" PRIu32 ")\n", s_vmmio_irq_count);
    s_vmmio_frame_pending = true;
}

static void hal_camera_reset(void)
{
    s_eth_dev.status = HAL_CAMERA_STATUS_INVALID;
    strncpy(s_eth_dev.name, "ETHERNET", sizeof(s_eth_dev.name));
    s_eth_dev.frame_width = 0;
    s_eth_dev.frame_height = 0;
    s_eth_dev.bytes_per_frame = 0;
    s_eth_dev.format = HAL_CAMERA_COLOUR_FORMAT_INVALID;
    s_eth_dev.mode = HAL_CAMERA_MODE_INVALID;
    s_eth_dev.buffer = NULL;
    s_eth_dev.buffer_size = 0;

    s_frame_seen = false;
}

bool hal_camera_init(void)
{
    hal_camera_reset();
    info("Initialising camera interface: %s\n", s_eth_dev.name);

#if defined(ISP_VIRTUAL_CAMERA_BASE_NS)
    /* Only present on MPS4-based designs (Corstone-315/320). Offsets right
     * after ispCC's MODE-like register at 0x0: configuring these with a
     * valid destination buffer before setting the enable bit is required --
     * enabling with no buffer configured previously corrupted memory
     * (reproduced and root-caused via ethernet/mem_probe.h). This block has
     * no effect on the actual frame path below (that's the vMMIO IRQ wait in
     * hal_camera_get_captured_frame(), reading data the DMA write already
     * placed at DYNAMIC_IFM_BASE) but is left in place, unchanged, as
     * already-proven-safe on platforms that have this peripheral at all. */
    {
        static const uint32_t buffer_addr_offsets[] = {0x4U, 0x8U, 0xCU, 0x10U};
        uint32_t k;
        bool ok = true;

        for (k = 0; k < 4U; k++) {
            bool wrote = mem_probe_write32(ISP_VIRTUAL_CAMERA_BASE_NS + buffer_addr_offsets[k],
                                            (uint32_t)DYNAMIC_IFM_BASE);
            if (!wrote) {
                printf_err("Failed to configure ispCC buffer register [0x%03" PRIx32 "]\n",
                            buffer_addr_offsets[k]);
            }
            ok = wrote && ok;
        }

        if (!mem_probe_write32(ISP_VIRTUAL_CAMERA_BASE_NS, 0x00000002U)) {
            printf_err("Failed to enable ispCC\n");
            ok = false;
        }

        if (!ok) {
            return false;
        }

        info("ispCC enabled, capture buffer at 0x%08" PRIx32 "\n", (uint32_t)DYNAMIC_IFM_BASE);
    }
#endif /* defined(ISP_VIRTUAL_CAMERA_BASE_NS) */

    /* Custom Atlas vMMIO entry: an external write anywhere in
     * [VMMIO_CAMERA_BASE, VMMIO_CAMERA_BASE + VMMIO_CAMERA_SIZE) fires
     * VMMIO_CAMERA_IRQ, so wire up a handler before anything can write there. */
    s_vmmio_frame_pending = false;
    NVIC_SetVector((IRQn_Type)VMMIO_CAMERA_IRQ, (uint32_t)VmmioCamera_Handler);
    NVIC_EnableIRQ((IRQn_Type)VMMIO_CAMERA_IRQ);
    info("vMMIO camera input at 0x%08" PRIx32 " (%" PRIu32 " bytes), IRQ %d\n",
         (uint32_t)VMMIO_CAMERA_BASE, (uint32_t)VMMIO_CAMERA_SIZE, (int)VMMIO_CAMERA_IRQ);

    return true;
}

bool hal_camera_configure(const uint32_t width,
                          const uint32_t height,
                          const hal_cam_mode mode,
                          const hal_cam_clr_format colour_format)
{
    if (HAL_CAMERA_STATUS_RUNNING == s_eth_dev.status) {
        printf_err("Camera is running; configuration failed\n");
        return false;
    }

    if (HAL_CAMERA_MODE_CONTINUOUS == mode) {
        printf_err("Only single shot mode is supported\n");
        return false;
    }

    s_eth_dev.frame_width = width;
    s_eth_dev.frame_height = height;
    s_eth_dev.mode = mode;
    s_eth_dev.format = colour_format;
    s_eth_dev.status = HAL_CAMERA_STATUS_STOPPED;

    switch (s_eth_dev.format) {
        case HAL_CAMERA_COLOUR_FORMAT_RGB888:
            s_eth_dev.bytes_per_frame = width * height * 3;
            break;
        case HAL_CAMERA_COLOUR_FORMAT_RGB565:
            s_eth_dev.bytes_per_frame = width * height * 2;
            break;
        default:
            printf_err("Unsupported colour format\n");
            hal_camera_reset();
            return false;
    }

    if (DYNAMIC_IFM_SIZE < s_eth_dev.bytes_per_frame) {
        printf_err("DYNAMIC_IFM_SIZE is too small\n");
        return false;
    }

    hal_camera_set_buffer((uint8_t*)(DYNAMIC_IFM_BASE), s_eth_dev.bytes_per_frame);
    info("Camera configured (Ethernet, %" PRIu32 "x%" PRIu32 ", %" PRIu32 " bytes/frame)\n",
         width, height, s_eth_dev.bytes_per_frame);
    return true;
}

bool hal_camera_set_buffer(uint8_t* buffer, const uint32_t size)
{
    if (size != s_eth_dev.bytes_per_frame) {
        return false;
    }

    s_eth_dev.buffer = buffer;
    s_eth_dev.buffer_size = size;
    return true;
}

bool hal_camera_start(void)
{
    if (s_eth_dev.status != HAL_CAMERA_STATUS_STOPPED) {
        debug("Camera is already running\n");
        return false;
    }

    s_frame_seen = false;
    s_vmmio_frame_pending = false;
    s_eth_dev.status = HAL_CAMERA_STATUS_RUNNING;
    return true;
}

const uint8_t* hal_camera_get_captured_frame(uint32_t* size)
{
    static unsigned int idx = 0;
    *size = 0;

    if (s_eth_dev.status != HAL_CAMERA_STATUS_RUNNING) {
        return NULL;
    }

    if (!s_frame_seen) {
        info("Waiting for the first frame over the network\n");
        s_frame_seen = true;
    }

    /* Every frame, including the first: wait for the vMMIO entry's IRQ. The
     * external peer's coreio_dma_write() has already landed the frame
     * directly in DYNAMIC_IFM_BASE (s_eth_dev.buffer) by the time it raises
     * this IRQ via coreio_irq_update() -- nothing needs to be read from
     * VMMIO_CAMERA_BASE, since that region is never written to at all. */
    while (!s_vmmio_frame_pending) {
    }
    s_vmmio_frame_pending = false;

    info("Frame: %u\n", idx);
    *size = s_eth_dev.bytes_per_frame;
    ++idx;

    return s_eth_dev.buffer;
}

bool hal_camera_stop(void)
{
    if (s_eth_dev.status == HAL_CAMERA_STATUS_RUNNING) {
        s_eth_dev.status = HAL_CAMERA_STATUS_STOPPED;
    }

    return true;
}

hal_cam_status hal_camera_get_status(void)
{
    return s_eth_dev.status;
}

void hal_camera_release(void)
{
    hal_camera_reset();
}

const char* hal_camera_get_device_name(void)
{
    return s_eth_dev.name;
}
