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

"""Standalone protocol smoke test for vsi_video_server.py, with no FVP required.

Spawns the synthetic-pattern video server as a subprocess and drives it with the
exact same command sequence the unmodified vsi_video.py client issues (SET_FILENAME,
STREAM_CONFIGURE, STREAM_ENABLE, FRAME_READ, STREAM_DISABLE, CLOSE_SERVER), then checks
that the frames it returns match the requested resolution and colour format byte-for-byte
in size. Requires opencv-python and numpy (the same dependencies documented for VSI in
docs/sections/deployment.md); it does not require an installed FVP.
"""

import argparse
import subprocess
import sys
import time
from multiprocessing.connection import Client
from pathlib import Path

SERVER_SCRIPT = Path(__file__).with_name("vsi_video_server.py")

# Command codes and colour-space constants, matching vsi_video_server.py's VideoServer.
SET_FILENAME, STREAM_CONFIGURE, STREAM_ENABLE, STREAM_DISABLE, FRAME_READ, FRAME_WRITE, CLOSE_SERVER = range(1, 8)
GRAYSCALE8, RGB888 = 1, 2
MODE_INPUT = 0

#: bytes-per-pixel for each colour format this test exercises.
_BYTES_PER_PIXEL = {GRAYSCALE8: 1, RGB888: 3}


def run_selftest(host, port, width, height, color_format, frame_count):
    """
    Drive the synthetic video server through one configure/enable/read/disable cycle.

    :param host:         Loopback address to bind the throwaway server on.
    :type host:          str
    :param port:          TCP port to bind the throwaway server on.
    :type port:          int
    :param width:         Requested frame width in pixels.
    :type width:          int
    :param height:        Requested frame height in pixels.
    :type height:         int
    :param color_format:  One of GRAYSCALE8 or RGB888.
    :type color_format:  int
    :param frame_count:   Number of frames to request and validate.
    :type frame_count:   int
    :raises AssertionError: If any returned frame is not exactly
                             width * height * bytes-per-pixel bytes long.
    :returns:              None
    """
    proc = subprocess.Popen(
        [sys.executable, str(SERVER_SCRIPT), "--ip", host, "--port", str(port), "--authkey", "vsi_video"]
    )
    time.sleep(1.0)

    try:
        conn = Client((host, port), authkey=b"vsi_video")

        conn.send([SET_FILENAME, "/tmp", "", MODE_INPUT])
        conn.recv()  # Empty filename is expected to be reported invalid; harmless (see README).

        conn.send([STREAM_CONFIGURE, width, height, color_format, 30])
        assert conn.recv() is True, "STREAM_CONFIGURE was rejected"

        conn.send([STREAM_ENABLE, MODE_INPUT])
        assert conn.recv() is True, "STREAM_ENABLE was rejected"

        expected_bytes = width * height * _BYTES_PER_PIXEL[color_format]
        for i in range(frame_count):
            conn.send([FRAME_READ])
            frame = conn.recv_bytes()
            eos = conn.recv()
            assert len(frame) == expected_bytes, (
                f"frame {i}: expected {expected_bytes} bytes, got {len(frame)}"
            )
            assert not eos, f"frame {i}: unexpected end-of-stream from an indefinite synthetic source"

        conn.send([STREAM_DISABLE])
        conn.recv()
        conn.send([CLOSE_SERVER])
        conn.close()
    finally:
        time.sleep(0.3)
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def parse_arguments():
    """
    Parse command-line arguments for the self-test.

    :returns: The parsed command-line arguments.
    :rtype:   argparse.Namespace
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=192,
                         help="Requested frame width in pixels (default: 192, matching "
                              "the object_detection yolo-fastest model)")
    parser.add_argument("--height", type=int, default=192,
                         help="Requested frame height in pixels (default: 192)")
    parser.add_argument("--port", type=int, default=6123,
                         help="Loopback TCP port for the throwaway server (default: 6123)")
    parser.add_argument("--frames", type=int, default=5,
                         help="Number of frames to request and validate (default: 5)")

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_arguments()
    run_selftest("127.0.0.1", args.port, args.width, args.height, GRAYSCALE8, args.frames)
    print(f"OK: {args.frames} synthetic frames of {args.width}x{args.height} GRAYSCALE8 read successfully")
