#----------------------------------------------------------------------------
#  SPDX-FileCopyrightText: Copyright 2024-2025 Arm Limited and/or
#  its affiliates <open-source-office@arm.com>
#  SPDX-License-Identifier: Apache-2.0
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#----------------------------------------------------------------------------

#########################################################
# Camera interface library                              #
#########################################################
cmake_minimum_required(VERSION 3.21.0)

project(hal_camera
    DESCRIPTION "Camera interface library"
    LANGUAGES   C)

add_library(hal_camera_interface INTERFACE)
target_include_directories(hal_camera_interface INTERFACE include)

option(HAL_CAMERA_LOOP "Loop static data infinitely" OFF)
add_library(hal_camera_static_images STATIC EXCLUDE_FROM_ALL)
target_sources(hal_camera_static_images PRIVATE
    source/hal_camera_static.c
    source/hal_camera_static_external.c)
target_include_directories(hal_camera_static_images PRIVATE source)
target_link_libraries(hal_camera_static_images PUBLIC hal_camera_interface hal_log)
target_compile_definitions(hal_camera_static_images PRIVATE
        $<$<BOOL:${HAL_CAMERA_LOOP}>:HAL_CAMERA_LOOP>)

if (${FVP_VSI_ENABLED})
    if (NOT DEFINED DYNAMIC_IFM_BASE OR NOT DEFINED DYNAMIC_IFM_SIZE)
        message(FATAL_ERROR "DYNAMIC_IFM_BASE and DYNAMIC_IFM_SIZE should be defined for VSI")
    endif()

    # Include VSI helper to bring in virtual_streaming_interface target.
    include(vsi)

    # Create HAL camera VSI library
    add_library(hal_camera_vsi STATIC EXCLUDE_FROM_ALL source/hal_camera_vsi.c)
    target_link_libraries(hal_camera_vsi PUBLIC
        hal_camera_interface
        hal_log
        virtual_streaming_interface)
    target_compile_definitions(hal_camera_vsi PRIVATE
        DYNAMIC_IFM_BASE=${DYNAMIC_IFM_BASE}
        DYNAMIC_IFM_SIZE=${DYNAMIC_IFM_SIZE})

    # Supress unused section linker warning for Arm Compiler
    if (CMAKE_CXX_COMPILER_ID STREQUAL "ARMClang")
        target_link_options(hal_camera_vsi INTERFACE
            --diag_suppress=L6329W)
    endif()
endif()

if (${ETHERNET_CAMERA_ENABLED})
    if (NOT DEFINED DYNAMIC_IFM_BASE OR NOT DEFINED DYNAMIC_IFM_SIZE)
        message(FATAL_ERROR "DYNAMIC_IFM_BASE and DYNAMIC_IFM_SIZE should be defined for ETHERNET_CAMERA_ENABLED")
    endif()

    # Address/size/IRQ of the custom "vMMIO entry" added in the Atlas
    # instance's UI (Devices -> Add vMMIO entry). Defaults pick
    # AHB_USER_0_BASE_NS (see peripheral_memmap.h) -- a genuinely unbacked
    # expansion slot on this sse-320 memory map, confirmed by probing (see
    # source/hal_camera_ethernet.c) -- and IRQ 60, inside a reserved,
    # otherwise-unused range in peripheral_irqs.h. Override with
    # -DVMMIO_CAMERA_BASE=/-DVMMIO_CAMERA_SIZE=/-DVMMIO_CAMERA_IRQ= if the
    # instance's vMMIO entry was created with different values.
    if (NOT DEFINED VMMIO_CAMERA_BASE)
        set(VMMIO_CAMERA_BASE 0x40104000)
    endif()
    if (NOT DEFINED VMMIO_CAMERA_SIZE)
        set(VMMIO_CAMERA_SIZE 0x20000)
    endif()
    if (NOT DEFINED VMMIO_CAMERA_IRQ)
        set(VMMIO_CAMERA_IRQ 60)
    endif()

    # Create HAL camera Ethernet library: the video feed arrives from the
    # Generic Linux instance over a custom vMMIO entry (see
    # source/hal_camera_ethernet.c) rather than through a VSI peripheral.
    add_library(hal_camera_ethernet STATIC EXCLUDE_FROM_ALL)
    target_sources(hal_camera_ethernet PRIVATE
        source/hal_camera_ethernet.c
        source/ethernet/mem_probe.c)
    target_include_directories(hal_camera_ethernet PRIVATE source)
    target_link_libraries(hal_camera_ethernet PUBLIC
        hal_camera_interface
        hal_log
        cmsis_device)
    target_compile_definitions(hal_camera_ethernet PRIVATE
        DYNAMIC_IFM_BASE=${DYNAMIC_IFM_BASE}
        DYNAMIC_IFM_SIZE=${DYNAMIC_IFM_SIZE}
        VMMIO_CAMERA_BASE=${VMMIO_CAMERA_BASE}
        VMMIO_CAMERA_SIZE=${VMMIO_CAMERA_SIZE}
        VMMIO_CAMERA_IRQ=${VMMIO_CAMERA_IRQ})
endif()

if (${TARGET_PLATFORM} STREQUAL alif)
    add_subdirectory(source/alif)
endif()

# Display status
message(STATUS "CMAKE_CURRENT_SOURCE_DIR: " ${CMAKE_CURRENT_SOURCE_DIR})
message(STATUS "*******************************************************")
message(STATUS "CMAKE_SYSTEM_PROCESSOR  : " ${CMAKE_SYSTEM_PROCESSOR})
message(STATUS "Library                 :  hal_camera_interface")
message(STATUS "*******************************************************")
