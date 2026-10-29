# Synthetic VSI video feed

This directory is a drop-in [Virtual Streaming Interface](https://arm-software.github.io/AVH/main/simulation/html/group__arm__vsi.html)
(VSI) `v_path` peer for Corstone FVPs that expose the AVH VSI peripheral (`ARM_VSI4`-`ARM_VSI7`),
used in place of `dependencies/avh/interface/video/python` to feed a deterministic synthetic
test pattern into a use case's camera input instead of a real webcam or video file. The VSI
protocol itself is identical across MPS3-based designs (Corstone-300, Corstone-310) and
MPS4-based designs (Corstone-315, Corstone-320) — see [VSI requirements](../../../docs/sections/deployment.md#vsi-requirements)
— so none of the Python here is board-specific; only the `v_path` prefix (`mps3_board.` vs
`mps4_board.`) and the firmware's build flags change per target.

It exists to close the gap the `object_detection` Corstone-320 baseline recorded in
[baselines/corstone-320/object-detection/2026-09-25-static-u85-z256-49ca1073/README.md](../../../baselines/corstone-320/object-detection/2026-09-25-static-u85-z256-49ca1073/README.md):
that baseline used the compiled-in static BMP samples (`FVP_VSI_ENABLED=OFF`) and
explicitly flagged "subsequent streaming work" as the next step. It is also used to feed the
Corstone-310 `object_detection` build (`TARGET_PLATFORM=mps3`, `TARGET_SUBSYSTEM=sse-310`,
`ETHOS_U_NPU_ID=U65`).

Corstone-310's own reference design ships a Cortex-M85 + Ethos-U65, not the Cortex-M55 +
Ethos-U55 of any real Alif Ensemble part — confirmed on real hardware-in-the-loop
(Corellium `cs310`): building with `ETHOS_U_NPU_ID=U55` boots the platform but fails at
`ethosu_dev_init()`'s product-ID check (`dependencies/core-driver/src/ethosu_device_u55_u65.c`),
because the NPU it actually finds at `0x50004000` reports itself as U65, not U55. `U65` is
the flag that boots; the resulting console confirms `Arch: v1.1.0` — the same architecture
generation as Ethos-U55 (Ethos-U85, used by Ensemble E4/E6/E8, is `Arch: v2.0.0`, a different
generation entirely). Of the Ensemble family, **E3** (2x Cortex-M55, no application core, one
256 MAC/cycle + one 128 MAC/cycle Ethos-U55) is the closest available stand-in: no other
series pairs an NPU of this throughput tier with an MCU-only (no Cortex-A32) part. It is a
throughput-class analogy, not a cycle-accurate substitute — the CPU (single M85 vs dual M55)
and NPU (U65 vs U55) are still different IP.

## What's here

| File | Origin |
|------|--------|
| `arm_vsi4.py`, `arm_vsi5.py`, `arm_vsi6.py`, `arm_vsi7.py` | Unmodified copies of `dependencies/avh/interface/video/python/arm_vsi*.py` |
| `vsi_video.py` | Unmodified copy of `dependencies/avh/interface/video/python/vsi_video.py` (the client the FVP spawns) |
| `vsi_video_server.py` | **Modified** copy: the camera-device branch (`cv2.VideoCapture(0)`) is replaced by `SyntheticPatternSource`, a dependency-free frame generator. Everything else (protocol, resize, colour-space conversion) is unchanged. |
| `selftest.py` | Standalone protocol smoke test; exercises the server without needing an FVP. |

The `dependencies/avh` submodule itself is never modified — changes there wouldn't
survive `git submodule update` and aren't appropriate to carry as local patches.
This directory is a full, independent `v_path` folder instead.

## Why a synthetic pattern

`vsi_video.py`'s `init()` locates `vsi_video_server.py` next to itself
(`path.dirname(__file__)`) and spawns it as a subprocess — so the server must live in
whatever directory `v_path` points at, and any behavioural change has to go there, not
in a wrapper. A synthetic pattern was chosen over a live webcam or a specific file so
the feed is fully reproducible (same frame index -> same bytes, every run) and needs
no camera hardware or a distributed video asset, matching the reproducibility this
repo's `baselines/` mechanism is built around.

`SyntheticPatternSource` renders a horizontally-scrolling, phase-shifted colour-bar
gradient with a frame-counter overlay at a 640x480 base resolution; the server's
existing resize/crop/colour-conversion pipeline (unchanged from the original) adapts
that to whatever resolution and colour format (`GRAYSCALE8`, `RGB888`, `BGR565`, ...)
the guest firmware requests via `STREAM_CONFIGURE`. It is a drop-in replacement for
`cv2.VideoCapture(0)` implementing only the subset of that interface the server
actually calls: `isOpened()`, `read()`, `release()`.

## Building the firmware to consume it

- Corstone-320 (`mps4`/`sse-320`, Ethos-U85):

```sh
cmake -S . -B build-cs320-vsi \
  -DCMAKE_TOOLCHAIN_FILE=scripts/cmake/toolchains/bare-metal-gcc.cmake \
  -DCMAKE_BUILD_TYPE=Release \
  -DTARGET_PLATFORM=mps4 \
  -DTARGET_SUBSYSTEM=sse-320 \
  -DETHOS_U_NPU_ID=U85 \
  -DUSE_CASE_BUILD=object_detection \
  -DCMSIS_DSP_MIN_REQ_SRC_LIST=ON \
  -DFVP_VSI_ENABLED=ON \
  -DFVP_VSI_SRC_PATH=dependencies/avh
cmake --build build-cs320-vsi --target mlek_object_detection --parallel 4
```

- Corstone-310 (`mps3`/`sse-310`, Ethos-U65 — see the note above on why `U65`, not `U55`):

```sh
cmake -S . -B build-cs310-vsi \
  -DCMAKE_TOOLCHAIN_FILE=scripts/cmake/toolchains/bare-metal-gcc.cmake \
  -DCMAKE_BUILD_TYPE=Release \
  -DTARGET_PLATFORM=mps3 \
  -DTARGET_SUBSYSTEM=sse-310 \
  -DETHOS_U_NPU_ID=U65 \
  -DUSE_CASE_BUILD=object_detection \
  -DCMSIS_DSP_MIN_REQ_SRC_LIST=ON \
  -DFVP_VSI_ENABLED=ON \
  -DFVP_VSI_SRC_PATH=dependencies/avh
cmake --build build-cs310-vsi --target mlek_object_detection --parallel 4
```

The `object_detection` use case has no pre-shipped U65 model under `resources_downloaded/`
(only `H128`/`H256` for U55 and `Z256` for U85) — the CMake configure step expects
`yolo-fastest_192_face_v4_vela_Y256.tflite` (`ETHOS_U_NPU_CONFIG_ID` defaults to `Y256` for
U65, per `scripts/cmake/configuration_options/npu_opts.cmake`) to already exist. Generate it
once with Vela before configuring:

```sh
cd resources_downloaded/object_detection
vela --accelerator-config ethos-u65-256 --optimise Performance --memory-mode Dedicated_Sram \
  --output-dir . yolo-fastest_192_face_v4.tflite
mv yolo-fastest_192_face_v4_vela.tflite yolo-fastest_192_face_v4_vela_Y256.tflite
```

Either way this links `hal_camera_vsi` (backed by `dependencies/avh/interface/video/source/video_drv.c`)
instead of `hal_camera_static_images`, so the firmware calls out over `ARM_VSI4`
MMIO/DMA/IRQ for every frame rather than reading a compiled-in BMP (confirm with
`arm-none-eabi-nm bin/mlek_object_detection.axf | grep VideoDrv_Initialize`). Do not reuse
`build-cs320` or `build-cs310` (the static-image builds) for this — keep streaming-enabled
builds in their own directory.

## Running it

Point the FVP's `v_path` at this directory instead of `dependencies/avh/interface/video/python`.
The board prefix differs by design: MPS4-based FVPs (Corstone-320) use `mps4_board.v_path`,
MPS3-based FVPs (Corstone-310) use `mps3_board.v_path` — see
[VSI requirements](../../../docs/sections/deployment.md#vsi-requirements).

- Corstone-320:

```sh
FVP_install_location/models/<os_arch>/FVP_Corstone_SSE-320 \
    -C mps4_board.subsystem.ethosu.num_macs=256 \
    -C mps4_board.visualisation.disable-visualisation=1 \
    -C vis_hdlcd.disable_visualisation=1 \
    -C mps4_board.telnetterminal0.start_telnet=0 \
    -C mps4_board.uart0.out_file='-' \
    -C mps4_board.v_path=scripts/vsi/synthetic_video \
    build-cs320-vsi/bin/mlek_object_detection.axf
```

- Corstone-310:

```sh
FVP_install_location/models/<os_arch>/FVP_Corstone_SSE-310 \
    -C mps3_board.subsystem.ethosu.num_macs=256 \
    -C mps3_board.visualisation.disable-visualisation=1 \
    -C mps3_board.telnetterminal0.start_telnet=0 \
    -C mps3_board.uart0.out_file='-' \
    -C mps3_board.v_path=scripts/vsi/synthetic_video \
    build-cs310-vsi/bin/mlek_object_detection.axf
```

  `num_macs=256` matches the `ETHOS_U_NPU_CONFIG_ID=Y256` build above.

Requires `opencv-python-headless` and `numpy` in the Python environment the FVP invokes
(`python3.9` per [docs/sections/deployment.md](../../../docs/sections/deployment.md);
see that doc's VSI Requirements section). Use the `-headless` package specifically,
not plain `opencv-python`: this server's synthetic-pattern path never opens a display
window or a real camera device, so it needs none of the GTK/GL bindings the full
package pulls in — and on a headless Linux box, `import cv2` from the full package
fails at `libGL.so.1: cannot open shared object file` before it ever gets to run
anything. If `opencv-python` is already installed, remove it first (`pip uninstall
opencv-python`) so the two don't conflict.

**Not exercised end-to-end against a local FVP binary in this environment**: no
Corstone-320 or Corstone-310 FVP binary is installed on this machine, so the
firmware-and-FVP half of this pipeline (the `-C ...v_path=...` invocations above)
could not be run here. What was verified locally for both targets:
- Both firmwares build cleanly with `FVP_VSI_ENABLED=ON` and link `hal_camera_vsi` /
  `video_drv.c` (confirmed via `nm` on each resulting `.axf`), not the static-image path.
- `vsi_video_server.py` itself was smoke-tested standalone (see `selftest.py`) by
  driving it with the exact command sequence `vsi_video.py` issues: `SET_FILENAME` ->
  `STREAM_CONFIGURE(192, 192, GRAYSCALE8, 30)` -> `STREAM_ENABLE` -> five `FRAME_READ`
  calls, each returning exactly 192*192 = 36864 bytes, matching the `object_detection`
  yolo-fastest model's `[1, 192, 192, 1]` int8 input contract shared by both the
  Corstone-320 baseline manifest and the Corstone-310/E3 build — the model and its input
  contract don't change between the two targets, only the NPU driving it.
- The Corstone-310 `object_detection` firmware (static-image build, `ETHOS_U_NPU_ID=U65`)
  was deployed and booted end-to-end on a real Corellium Atlas `cs310` instance: Ethos-U
  device initialised (`Arch: v1.1.0`, `MACs/cc: 256`), all four sample images ran through
  the NPU, and detection boxes were produced with the expected confidences — see
  [baselines/corstone-320](../../../baselines/corstone-320) for the analogous Corstone-320
  precedent. Whether Corellium's `cs310` flavor exposes the VSI peripheral the same way a
  local FVP's `v_path` argument does, and how a `v_path`-style Python peer would be wired
  to it, has not been established — this README's "Running it" commands are written for a
  locally-installed Arm FVP binary, not for Corellium.

Run `python3 scripts/vsi/synthetic_video/selftest.py` (with `opencv-python` and `numpy`
installed) to re-verify that any future change to `vsi_video_server.py` still behaves
correctly, without needing an FVP.
