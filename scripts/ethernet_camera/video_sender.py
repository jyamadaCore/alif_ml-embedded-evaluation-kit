#!/usr/bin/env python3
#  SPDX-FileCopyrightText:  Copyright 2026 Arm Limited and/or
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

"""Feeds a synthetic test pattern, or a real video file, to a Corstone instance
over Corellium's CoreIO/ExtIO protocol.

Run on the Generic Linux instance, with the `coreio` package installed
(https://support.avh.corellium.com/files/coreio.tar.gz -- `pip3 install .`
from its top-level directory). Pass --video to stream an actual video file
(requires opencv-python) instead of the default synthetic colour-bar pattern;
it loops back to the start once the file ends.

A vMMIO entry (Devices -> Add vMMIO entry in the Atlas instance UI) is a
*trapped* MMIO region: the guest's individual load/store instructions against
it are relayed one at a time to a registered read/write handler, not a block
of memory an external peer can blit bytes into over a plain socket -- an
earlier version of this script did exactly that and silently delivered
nothing, every time, despite the TCP `send()` calls all succeeding. Bulk frame
data instead goes over `coreio.dma_write()`, straight into real guest DRAM at
DYNAMIC_IFM_BASE, bypassing the vMMIO trap entirely. The vMMIO entry's only
remaining job is to carry an IRQ line: firing it is a *separate* operation
from the data write (`coreio.irq_update()`, toggled 1 then 0 for an edge),
matching what source/hal/source/components/camera/source/hal_camera_ethernet.c
now expects. See https://support.avh.corellium.com/device-settings/vmmio.
"""

import argparse
import time

import coreio
import numpy as np

BASE_WIDTH = 640
BASE_HEIGHT = 480

# Fixed TCP port AVH devices use for vMMIO/CoreIO connections. Set the vMMIO
# entry's "Port" field to this value in the Atlas UI -- it is not a port Atlas
# assigns and shows back to you afterwards.
DEFAULT_COREIO_PORT = 4300

# DYNAMIC_MODEL_BASE (0x90000000) + DYNAMIC_MODEL_SIZE (0x02000000) from the
# mps3/mps4 CMake defaults (scripts/cmake/platforms/*/build_configuration.cmake
# and the platform CMakeLists.txt files) -- override with --ifm-base if the
# firmware was configured with different values.
DEFAULT_IFM_BASE = 0x92000000


def render_pattern_frame(width, height, frame_index):
    """
    Render one synthetic RGB888 test-pattern frame.

    :param width:       Frame width in pixels.
    :type width:        int
    :param height:      Frame height in pixels.
    :type height:       int
    :param frame_index: Monotonically increasing frame counter used to animate
                         the pattern; the same index always renders the same frame.
    :type frame_index:  int
    :returns:           RGB888 frame, shape (height, width, 3), dtype uint8.
    :rtype:              numpy.ndarray
    """
    shift = frame_index % width
    ramp = (np.arange(width, dtype=np.int32) + shift) % width
    bar = (ramp * 255 // max(width - 1, 1)).astype(np.uint8)

    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 0] = bar
    frame[:, :, 1] = np.roll(bar, width // 3)
    frame[:, :, 2] = np.roll(bar, (2 * width) // 3)

    return frame


def resize_nearest(frame, width, height):
    """
    Resize a frame using nearest-neighbour sampling.

    :param frame:  Source RGB888 frame.
    :type frame:   numpy.ndarray
    :param width:  Target width in pixels.
    :type width:   int
    :param height: Target height in pixels.
    :type height:  int
    :returns:      Resized RGB888 frame.
    :rtype:        numpy.ndarray
    """
    src_h, src_w = frame.shape[:2]
    row_idx = np.arange(height) * src_h // height
    col_idx = np.arange(width) * src_w // width
    return frame[row_idx][:, col_idx]


class SyntheticPatternSource:
    """
    Deterministic, dependency-free colour-bar test pattern.

    Same frame index always renders the same bytes; useful for confirming the
    delivery pipeline works before pointing it at a real video file.
    """

    def __init__(self):
        self._frame_id = 0

    def next_frame(self, width, height):
        """
        Render the next synthetic frame.

        :param width:  Frame width in pixels.
        :type width:   int
        :param height: Frame height in pixels.
        :type height:  int
        :returns:      RGB888 frame, shape (height, width, 3), dtype uint8.
        :rtype:        numpy.ndarray
        """
        frame = render_pattern_frame(BASE_WIDTH, BASE_HEIGHT, self._frame_id)
        frame = resize_nearest(frame, width, height)
        self._frame_id += 1
        return frame


class VideoFileSource:
    """
    A real video file, decoded frame by frame via OpenCV.

    Loops back to the start once the file ends, so a short clip can drive an
    indefinitely long inference run.
    """

    def __init__(self, path):
        """
        Open a video file for reading.

        :param path: Path to the video file.
        :type path:  str
        :raises RuntimeError: If the file cannot be opened.
        """
        import cv2  # Imported lazily: only needed when --video is used.

        self._cv2 = cv2
        self._path = path
        self._cap = cv2.VideoCapture(path)
        if not self._cap.isOpened():
            raise RuntimeError(f"Could not open video file: {path}")

    def next_frame(self, width, height):
        """
        Decode and return the next frame, looping back to the start at EOF.

        :param width:  Target frame width in pixels.
        :type width:   int
        :param height: Target frame height in pixels.
        :type height:  int
        :returns:      RGB888 frame, shape (height, width, 3), dtype uint8.
        :rtype:        numpy.ndarray
        :raises RuntimeError: If no frame can be read even after looping.
        """
        ok, frame_bgr = self._cap.read()
        if not ok:
            self._cap.set(self._cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame_bgr = self._cap.read()
            if not ok:
                raise RuntimeError(f"Could not read any frames from video file: {self._path}")

        frame_rgb = self._cv2.cvtColor(frame_bgr, self._cv2.COLOR_BGR2RGB)
        return self._cv2.resize(frame_rgb, (width, height), interpolation=self._cv2.INTER_AREA)


def _noop_read(funcp, addr, data, flags):
    """
    Service a guest read of the registered vMMIO range.

    Nothing is ever read from this range by the firmware (frames arrive via
    DMA into DYNAMIC_IFM_BASE instead) but coreio.register() requires a
    read handler to be supplied regardless.

    :param funcp: Opaque handler parameter passed to coreio.register() (unused).
    :type funcp:  object
    :param addr:  Guest-relative offset within the registered range.
    :type addr:   int
    :param data:  Bytearray the handler may fill with response data.
    :type data:   bytearray
    :param flags:  Access flags (privilege/security/instruction-fetch bits).
    :type flags:   int
    :returns:      0 on success.
    :rtype:        int
    """
    return 0


def _noop_write(funcp, addr, data, flags):
    """
    Service a guest write into the registered vMMIO range.

    Nothing is ever written into this range by the firmware; see
    :func:`_noop_read` for why a handler is still required.

    :param funcp: Opaque handler parameter passed to coreio.register() (unused).
    :type funcp:  object
    :param addr:  Guest-relative offset within the registered range.
    :type addr:   int
    :param data:  Bytearray containing the bytes the guest wrote.
    :type data:   bytearray
    :param flags:  Access flags (privilege/security/instruction-fetch bits).
    :type flags:   int
    :returns:      0 on success.
    :rtype:        int
    """
    return 0


def send_frame(ifm_base, rid, iid, frame_bytes):
    """
    Deliver one frame to the guest.

    DMAs the frame directly into guest DRAM, then raises the vMMIO entry's
    IRQ as an edge. coreio.irq_update() is a separate operation from the data
    write -- a write alone never fires an interrupt on the guest.

    :param ifm_base:    Guest physical address to DMA the frame into.
    :type ifm_base:     int
    :param rid:         vMMIO range ID associated with the IRQ line.
    :type rid:          int
    :param iid:         IRQ index within that vMMIO range.
    :type iid:          int
    :param frame_bytes: Raw frame bytes to write.
    :type frame_bytes:  bytearray
    :returns:           0 on success; the first non-zero error code encountered otherwise.
    :rtype:              int
    """
    err = coreio.dma_write(mid=0, sid=0, addr=ifm_base, buf=frame_bytes, flags=0)
    if err == 0:
        err = coreio.irq_update(rid=rid, iid=iid, state=1)
    if err == 0:
        err = coreio.irq_update(rid=rid, iid=iid, state=0)
    return err


class _CoreioSession:
    """
    Owns a CoreIO connection and transparently reconnects it after a disconnect.

    The underlying TCP connection to Atlas has been observed to drop
    periodically during a long-running stream (`[corehdl] Disconnected from
    host.` on stderr); coreio itself does not retry, so the caller must.

    Deliberately single-threaded: an earlier version ran coreio.mainloop() on
    a background thread, serialised against the main thread's dma_write()/
    irq_update() calls with a lock. That reintroduced the exact problem it
    was meant to avoid -- the pump thread's tight
    `while: with lock: mainloop(100000)` loop re-acquires the lock again
    almost immediately after releasing it, and CPython's lock makes no
    fairness guarantee, so the main thread's attempt to acquire the same lock
    for a single dma_write() call could (and did, reproducibly, right after
    an instance restart) starve indefinitely. A minimal single-threaded
    reproduction -- connect, register, one dma_write(), two irq_update()
    calls, no thread, no lock -- completed in under 0.1s every time. Pumping
    mainloop() inline, once per frame in the same thread that sends frames,
    gets the same keepalive/servicing behaviour with no lock and no
    starvation risk.
    """

    def __init__(self, ip, port, rid):
        """
        Connect and register the vMMIO range handler.

        :param ip:   Corstone instance's device/services IP.
        :type ip:    str
        :param port: CoreIO/ExtIO TCP port.
        :type port:  int
        :param rid:  vMMIO range ID to register.
        :type rid:   int
        """
        self._ip = ip
        self._port = port
        self._rid = rid
        self._connect()

    def _connect(self):
        """
        Establish the connection and register the vMMIO range handler.

        :raises RuntimeError: If coreio.connect() fails.
        :returns:             None
        """
        if coreio.connect(target=f"{self._ip}:{self._port}") != 0:
            raise RuntimeError(f"coreio.connect to {self._ip}:{self._port} failed")
        coreio.register(rid=self._rid, read=_noop_read, write=_noop_write, funcp=None)

    def reconnect(self):
        """
        Tear down and re-establish the connection after a detected failure.

        :returns: None
        """
        coreio.disconnect()
        self._connect()

    def pump(self):
        """
        Non-blocking service of the connection (keepalive pings, any
        (unused) vMMIO range accesses). Call once per frame from the same
        thread that sends frames -- see the class docstring for why this
        must not run concurrently on a separate thread.

        :returns: 0 on success; non-zero if the connection has failed.
        :rtype:   int
        """
        return coreio.mainloop(0)

    def send(self, ifm_base, iid, frame_bytes):
        """
        Deliver one frame using this session's range ID.

        :param ifm_base:    Guest physical address to DMA the frame into.
        :type ifm_base:     int
        :param iid:         IRQ index within the registered vMMIO range.
        :type iid:          int
        :param frame_bytes: Raw frame bytes to write.
        :type frame_bytes:  bytearray
        :returns:           0 on success; non-zero error code otherwise.
        :rtype:              int
        """
        return send_frame(ifm_base, self._rid, iid, frame_bytes)

    def close(self):
        """
        Disconnect.

        :returns: None
        """
        coreio.disconnect()


def run(ip, port, ifm_base, rid, iid, width, height, fps, frame_source):
    """
    Connect over CoreIO and stream frames continuously until interrupted.

    :param ip:           Corstone instance's device/services IP.
    :type ip:            str
    :param port:         CoreIO/ExtIO TCP port (default: 4300).
    :type port:          int
    :param ifm_base:     Guest physical address of DYNAMIC_IFM_BASE.
    :type ifm_base:      int
    :param rid:          vMMIO range ID, in order of registration in the Atlas UI.
    :type rid:           int
    :param iid:          IRQ index within that vMMIO range.
    :type iid:           int
    :param width:        Frame width in pixels.
    :type width:         int
    :param height:       Frame height in pixels.
    :type height:        int
    :param fps:          Target frames per second.
    :type fps:           float
    :param frame_source: Source to pull frames from (synthetic pattern or video file).
    :type frame_source:  SyntheticPatternSource | VideoFileSource
    :returns:            None
    """
    session = _CoreioSession(ip, port, rid)

    print(f"Connected to {ip}:{port} (CoreIO). DMA-streaming {width}x{height} RGB888 into "
          f"0x{ifm_base:08x}, IRQ rid={rid} iid={iid}, at ~{fps:.1f} fps. Ctrl-C to stop.",
          flush=True)

    frame_id = 0
    period = 1.0 / fps
    consecutive_failures = 0

    try:
        while True:
            t0 = time.monotonic()

            session.pump()

            frame = frame_source.next_frame(width, height)
            frame_bytes = bytearray(np.ascontiguousarray(frame).tobytes())
            err = session.send(ifm_base, iid, frame_bytes)

            if err != 0:
                consecutive_failures += 1
                print(f"  frame {frame_id}: send failed (err={err}); reconnecting "
                      f"(attempt {consecutive_failures})...", flush=True)
                backoff = min(5.0, 0.5 * consecutive_failures)
                time.sleep(backoff)
                try:
                    session.reconnect()
                    print(f"  reconnected to {ip}:{port}", flush=True)
                except RuntimeError as exc:
                    print(f"  reconnect failed: {exc}", flush=True)
                continue  # Retry this same frame_id once reconnected.

            consecutive_failures = 0
            print(f"  frame {frame_id}: DMA'd {len(frame_bytes)} bytes, IRQ raised", flush=True)
            frame_id += 1
            elapsed = time.monotonic() - t0
            if elapsed < period:
                time.sleep(period - elapsed)
    except KeyboardInterrupt:
        pass
    finally:
        session.close()


def parse_arguments():
    """
    Parse command-line arguments for the sender.

    :returns: The parsed command-line arguments.
    :rtype:   argparse.Namespace
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ip", required=True, help="Corstone instance's device/services IP")
    parser.add_argument("--port", type=int, default=DEFAULT_COREIO_PORT,
                         help="CoreIO/ExtIO TCP port configured for the vMMIO entry "
                              f"(default: {DEFAULT_COREIO_PORT}, the fixed AVH vMMIO port -- "
                              "set the entry's 'Port' field to this value in the Atlas UI, "
                              "it is not something Atlas assigns and reports back)")
    parser.add_argument("--ifm-base", type=lambda s: int(s, 0), default=DEFAULT_IFM_BASE,
                         help="Guest physical address of DYNAMIC_IFM_BASE to DMA frames into "
                              f"(default: 0x{DEFAULT_IFM_BASE:08x})")
    parser.add_argument("--rid", type=int, default=0,
                         help="vMMIO range ID, in order of registration in the Atlas UI (default: 0)")
    parser.add_argument("--iid", type=int, default=0,
                         help="IRQ index within that vMMIO range (default: 0)")
    parser.add_argument("--width", type=int, default=192, help="Frame width in pixels (default: 192)")
    parser.add_argument("--height", type=int, default=192, help="Frame height in pixels (default: 192)")
    parser.add_argument("--fps", type=float, default=5.0, help="Target frames per second (default: 5.0)")
    parser.add_argument("--video", type=str, default=None,
                         help="Path to a video file to stream instead of the synthetic test "
                              "pattern (loops back to the start once it ends). Requires "
                              "opencv-python.")

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_arguments()
    source = VideoFileSource(args.video) if args.video else SyntheticPatternSource()
    run(args.ip, args.port, args.ifm_base, args.rid, args.iid, args.width, args.height, args.fps, source)
