# vMMIO-fed camera for Corstone targets with no VSI or NIC

This is the video-feed path for targets (such as this project's Corellium Atlas
Corstone-320 and Corstone-310 configurations) that expose **no VSI peripheral
and no guest-visible NIC** at all. It replaces
[scripts/vsi/synthetic_video/](../vsi/synthetic_video/) (no VSI region on
these targets) and two earlier attempts that turned out unbacked on Corstone-320:
`ETHERNET_BASE_NS`/`_S` fault on the first register read, and the instance's
LAN IP gets `Destination Host Unreachable` back from the sender's own
gateway -- consistent with no NIC being modelled for that guest at all.

## What's here

| File | Role |
|------|------|
| `video_sender.py` | Runs on the Generic Linux instance. Renders a synthetic test pattern, DMAs each frame directly into guest DRAM via Corellium's `coreio` client library, then raises the vMMIO entry's IRQ as an edge. |
| (firmware) [`hal_camera_ethernet.c`](../../source/hal/source/components/camera/source/hal_camera_ethernet.c) | `hal_camera.h` backend: waits for the vMMIO entry's IRQ, then reads the frame the DMA write already placed at `DYNAMIC_IFM_BASE` directly -- no copy needed. On MPS4-based designs only, also configures ispCC's (`ISP_VIRTUAL_CAMERA_BASE_NS`) capture buffer and enables it (vestigial for the actual frame path, kept as already-proven-safe). |
| (firmware) [`ethernet/mem_probe.c/.h`](../../source/hal/source/components/camera/source/ethernet/) | Fault-tolerant single-word read/write, used only for the ispCC configure/enable sequence (live peripheral registers). |

## How it works

The Atlas instance UI has a "Devices -> Add vMMIO entry" panel (Start / Size /
IRQ / Port) for wiring a custom memory-mapped region into the guest's address
space, driven externally over Corellium's CoreIO/ExtIO protocol -- see
[AVH vMMIO](https://support.avh.corellium.com/device-settings/vmmio) and the
`coreio` client package at
[support.avh.corellium.com/files/coreio.tar.gz](https://support.avh.corellium.com/files/coreio.tar.gz).

**A vMMIO entry's address range is a *trapped* MMIO region, not memory an
external peer can blit bytes into.** Every load/store instruction the guest
executes against `[Start, Start + Size)` is relayed one access at a time
(1, 2, 4 or 8 bytes) to a handler an external client registers over CoreIO
(`coreio.register()`). An earlier version of `video_sender.py` didn't know
this: it just opened a plain TCP socket to the entry's port and wrote raw
frame bytes, back to back, with no framing at all. Every `send()` succeeded,
nothing was ever delivered, and the firmware sat at "Waiting for the first
frame over the network" forever -- because that raw byte stream was never a
CoreIO client to begin with, so nothing was listening for it to be one.
Critically, **firing an IRQ is a separate operation from writing data**:
`coreio.irq_update()` must be called explicitly, toggled `1` then `0` for an
edge; a plain register write, even a correctly-framed one, never fires an
interrupt on its own.

Bulk data (a whole video frame) is a poor fit for that one-access-at-a-time
trap model regardless of framing. Instead, `video_sender.py`:

1. Connects with `coreio.connect(f"{ip}:4300")` -- **4300 is the fixed AVH
   vMMIO port**; set the entry's own "Port" field to `4300` in the Atlas UI,
   it is not a port Atlas assigns you afterwards.
2. Calls `coreio.register(rid=0, read=..., write=...)` once, with no-op
   handlers -- required by the API, but nothing the firmware does ever
   actually reads or writes this range for data.
3. For each frame, calls `coreio.dma_write(mid=0, sid=0, addr=DYNAMIC_IFM_BASE,
   buf=frame_bytes, flags=0)` -- a **direct DMA write into real guest DRAM**,
   bypassing the vMMIO trap entirely (`mid=0` selects "direct", i.e. no IOMMU
   translation).
4. Then calls `coreio.irq_update(rid=0, iid=0, state=1)` followed immediately
   by `state=0`, raising `VMMIO_CAMERA_IRQ` on the guest as an edge.

On the firmware side, `hal_camera_get_captured_frame()` waits for that IRQ,
then returns `DYNAMIC_IFM_BASE` directly -- the DMA write already placed the
frame there, so no copy from `VMMIO_CAMERA_BASE` is needed (nothing is ever
written to that address at all; it exists purely so the vMMIO entry has an
IRQ line to carry). This is deliberately not gated on the buffer's initial
content: `DYNAMIC_IFM_BASE` is confirmed all-zero at boot, but waiting for
"first non-zero byte" instead of the IRQ would (and did, in an earlier
version) pass immediately on startup, before the sender had even connected.

On MPS4-based designs (Corstone-315/320) only, `hal_camera_init()` also:

1. Writes `DYNAMIC_IFM_BASE` into ispCC's buffer-address registers
   (`ISP_VIRTUAL_CAMERA_BASE_NS + 0x4/0x8/0xC/0x10`) -- confirmed required
   before enabling; an enable with no destination configured previously
   corrupted memory.
2. Sets ispCC's enable bit (`ISP_VIRTUAL_CAMERA_BASE_NS + 0x0 = 0x2`).

This has no effect on the frame path above; it's vestigial from an earlier
iteration and kept, unchanged, as already-proven-safe. MPS3-based designs
(Corstone-300/310) have no such peripheral, so this step is skipped there
(`#if defined(ISP_VIRTUAL_CAMERA_BASE_NS)`).

## Installing the `coreio` Python package

On the Generic Linux instance that will run `video_sender.py`:

```sh
curl -O https://support.avh.corellium.com/files/coreio.tar.gz
tar xzf coreio.tar.gz
cd coreio
pip3 install .
```

This builds and installs the `coreio` C-extension module (Apache-2.0
licensed by Corellium, matching this repository's license) that
`video_sender.py` imports.

## Building the firmware

- Corstone-320 (`mps4`/`sse-320`, Ethos-U85):

```sh
cmake -S . -B build-cs320-eth \
  -DCMAKE_TOOLCHAIN_FILE=scripts/cmake/toolchains/bare-metal-gcc.cmake \
  -DCMAKE_BUILD_TYPE=Release \
  -DTARGET_PLATFORM=mps4 \
  -DTARGET_SUBSYSTEM=sse-320 \
  -DETHOS_U_NPU_ID=U85 \
  -DUSE_CASE_BUILD=object_detection \
  -DCMSIS_DSP_MIN_REQ_SRC_LIST=ON \
  -DETHERNET_CAMERA_ENABLED=ON
cmake --build build-cs320-eth --target mlek_object_detection --parallel 4
```

- Corstone-310 (`mps3`/`sse-310`, Ethos-U65 -- see
  [scripts/vsi/synthetic_video/README.md](../vsi/synthetic_video/README.md)
  for why `U65`, not the Alif Ensemble E3's native `U55`):

```sh
cmake -S . -B build-cs310-eth \
  -DCMAKE_TOOLCHAIN_FILE=scripts/cmake/toolchains/bare-metal-gcc.cmake \
  -DCMAKE_BUILD_TYPE=Release \
  -DTARGET_PLATFORM=mps3 \
  -DTARGET_SUBSYSTEM=sse-310 \
  -DETHOS_U_NPU_ID=U65 \
  -DUSE_CASE_BUILD=object_detection \
  -DCMSIS_DSP_MIN_REQ_SRC_LIST=ON \
  -DETHERNET_CAMERA_ENABLED=ON
cmake --build build-cs310-eth --target mlek_object_detection --parallel 4
```

  Needs `yolo-fastest_192_face_v4_vela_Y256.tflite` under
  `resources_downloaded/object_detection/` first -- see the Vela command in
  the Corstone-310 section of
  [scripts/vsi/synthetic_video/README.md](../vsi/synthetic_video/README.md).

Either way, `ETHERNET_CAMERA_ENABLED` and `FVP_VSI_ENABLED` are mutually
exclusive -- both select a `hal_camera` backend in
[camera/CMakeLists.txt](../../source/hal/source/components/camera/CMakeLists.txt),
which also defines `VMMIO_CAMERA_BASE` (default `0x40104000`),
`VMMIO_CAMERA_SIZE` (default `0x20000`), and `VMMIO_CAMERA_IRQ` (default `60`).
Pass `-DVMMIO_CAMERA_BASE=...`/`-DVMMIO_CAMERA_SIZE=...`/`-DVMMIO_CAMERA_IRQ=...`
to override any of these if the vMMIO entry ends up needing different values.

## Running it

In the Atlas instance UI, add a vMMIO entry with Start `0x40104000`, Size
`0x20000`, IRQ `60` (or whatever values you built the firmware with), and
**Port `4300`** -- that last field is easy to miss and is not something Atlas
fills in or reports back to you; it must match `--port` below (default 4300).

Load the firmware onto the instance and start it, then on the Generic Linux
instance:

```sh
python3 scripts/ethernet_camera/video_sender.py --ip <device-ip> --width 192 --height 192 --fps 5
```

`--ip` is the target instance's device/services IP; `--port` (4300),
`--ifm-base` (`0x92000000`), `--rid` (`0`) and `--iid` (`0`) all default to
values matching the build/entry settings above and shouldn't need changing
unless you overrode one of them. Order between firmware boot and sender start
doesn't matter -- `hal_camera_get_captured_frame()` waits for the IRQ either
way.

`object_detection`'s `while (true)` loop in `UseCaseHandler.cc` keeps calling
`hal_camera_start()`/`hal_camera_get_captured_frame()` and running inference
on every frame for as long as the sender keeps sending them -- "single shot"
in `hal_camera_configure()`'s mode argument just means one explicit capture
request per call, not "one frame total". So this already streams and infers
continuously; the console will keep printing `Frame: N` and a fresh inference
result for as long as `video_sender.py` keeps running.

To feed a real video instead of the synthetic pattern, pass `--video`:

```sh
python3 scripts/ethernet_camera/video_sender.py --ip <device-ip> --video path/to/clip.mp4
```

This decodes the file frame-by-frame with OpenCV, converts BGR->RGB and
resizes to `--width`x`--height`, and loops back to the start once the file
ends -- so a short clip can drive an indefinitely long run. Frame rate is
still paced by `--fps`, independent of the source file's own frame rate.

Install with `pip3 install opencv-python-headless`, not plain `opencv-python`:
this script never opens a display window, so it needs none of the GTK/GL
bindings the full package pulls in -- on a headless Linux box, `import cv2`
from the full package fails at `libGL.so.1: cannot open shared object file`
before it gets anywhere near reading a frame. If `opencv-python` is already
installed, remove it first (`pip3 uninstall opencv-python`) so the two don't
conflict -- same issue, same fix, as
[scripts/vsi/synthetic_video/README.md](../vsi/synthetic_video/README.md)
already flags for its own VSI dependency.

### Reconnecting on disconnect

The underlying TCP connection to Atlas has been observed to drop mid-stream
during a long-running video (`[corehdl] Disconnected from host.` on stderr --
that's `corehdl-base.c` reporting a `recv()` returning 0/error on the control
or DMA socket; `coreio` itself does not retry). `video_sender.py` checks the
return code of every `dma_write()`/`irq_update()` call -- it used to just
call them and print success unconditionally, which silently hid exactly this
failure -- and on a non-zero return, disconnects, reconnects, re-registers
the vMMIO range, and retries the *same* frame, with a short backoff between
attempts. Watch for `send failed (err=...); reconnecting` in its output; if
that shows up repeatedly, the connection itself is flaky, not the pipeline.

## What's verified vs. assumed

**Verified locally:** both the Corstone-320 and Corstone-310 firmware builds
link cleanly with `ETHERNET_CAMERA_ENABLED=ON`. `video_sender.py`'s frame
rendering/resizing (both the synthetic pattern and `--video` paths) is
verified against the exact 192x192x3=110592-byte frame size the
object_detection model expects, and `--video`'s looping is verified against a
short locally-generated test clip.

**Verified against a real Corstone-310 instance, end to end, with a real
video:** streaming the synthetic pattern first showed `vMMIO camera IRQ fired
(count=N)` incrementing frame over frame on the firmware console, each
followed by `Frame: N`, a full inference, and NPU profiling output
(`NPU ACTIVE`/`IDLE`/`TOTAL` cycles) -- confirming the pipeline (CoreIO
connect -> DMA write -> IRQ edge -> guest wakes -> inference) running
continuously, not just once. Then, with `--video` pointed at a real face
video, the firmware produced a genuine detection (`Detection box:
{x=96,y=17,w=27,h=53}` at 0.995 confidence) on a real decoded frame -- visible
in the framebuffer as the source image with a bounding box drawn around the
detected face, not the synthetic colour bars.

The stream did stall partway through a ~1-minute clip, traced to the
`[corehdl] Disconnected from host.` issue above; the reconnect logic that
followed has been verified locally against a stub that simulates a mid-stream
disconnect (correctly detects the failure, reconnects, and retries the same
frame without skipping it) but not yet re-verified against a live instance
for a full, uninterrupted run.

**Previously verified against real instances (superseded):** the earlier,
raw-byte-stream version of this script connected successfully and streamed
100+ frames of the correct size to a real vMMIO entry's TCP port on both a
Corstone-320 and a
Corstone-310 instance, with zero corresponding IRQs ever observed on the
guest (confirmed via a firmware-side counter printed on every IRQ firing,
independent of any other logic) -- conclusively ruling out the firmware's
IRQ registration/handling as the cause, and pointing at the wire protocol
itself. That is what led to finding the actual CoreIO/ExtIO protocol
documented above.

If frames ever stop arriving again on a new instance, first suspect the vMMIO
entry's **Port** field not actually being set to `4300` in the Atlas UI (it
defaults to something else and is easy to miss), followed by `rid`/`iid` not
matching the entry's registration order if more than one custom vMMIO entry
exists on the instance.
