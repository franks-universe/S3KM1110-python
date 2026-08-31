#!/usr/bin/env python3
"""Live S3KM1110 Debug-mode profile and heatmap viewer."""

from __future__ import annotations

import argparse
import csv
import queue
import signal
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from s3km1110_tool import switch_mode


DEBUG_HEADER = bytes.fromhex("AA BF 10 14")
DEBUG_TAIL = bytes.fromhex("FD FC FB FA")
DEBUG_VALUE_COUNT = 320
DEBUG_FRAME_SIZE = 4 + (DEBUG_VALUE_COUNT * 4) + 4
DEBUG_CENTER_BIN = 160


@dataclass(frozen=True)
class DebugFrame:
    timestamp: float
    monotonic: float
    values: tuple[int, ...]
    raw: bytes


class DebugParser:
    def __init__(self) -> None:
        self._buffer = bytearray()
        self.invalid_frames = 0

    def feed(self, data: bytes) -> list[DebugFrame]:
        self._buffer.extend(data)
        frames: list[DebugFrame] = []

        while True:
            start = self._buffer.find(DEBUG_HEADER)
            if start < 0:
                keep = min(len(self._buffer), len(DEBUG_HEADER) - 1)
                if keep:
                    del self._buffer[:-keep]
                else:
                    self._buffer.clear()
                break
            if start:
                del self._buffer[:start]
            if len(self._buffer) < DEBUG_FRAME_SIZE:
                break

            raw = bytes(self._buffer[:DEBUG_FRAME_SIZE])
            if raw[-4:] != DEBUG_TAIL:
                self.invalid_frames += 1
                del self._buffer[0]
                continue
            del self._buffer[:DEBUG_FRAME_SIZE]
            frames.append(
                DebugFrame(
                    timestamp=time.time(),
                    monotonic=time.monotonic(),
                    values=struct.unpack("<320I", raw[4:-4]),
                    raw=raw,
                )
            )

        return frames


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="serial port, such as COM11")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--bin", type=int, choices=range(320), default=176)
    parser.add_argument("--window", type=float, default=60.0, help="rolling seconds")
    parser.add_argument("--raw", type=Path, help="write lossless Debug frames")
    parser.add_argument(
        "--append",
        action="store_true",
        help="append to --raw instead of replacing it (timestamps still describe this run only)",
    )
    parser.add_argument(
        "--timestamps",
        type=Path,
        help="write frame index and precise receive timestamps to CSV",
    )
    return parser.parse_args()


def capture_worker(args, frames, stopped, errors) -> None:
    raw_file = None
    timestamp_file = None
    try:
        import serial

        if args.raw:
            args.raw.parent.mkdir(parents=True, exist_ok=True)
            raw_file = args.raw.open("ab" if args.append else "wb")
        timestamp_writer = None
        if args.timestamps:
            args.timestamps.parent.mkdir(parents=True, exist_ok=True)
            timestamp_file = args.timestamps.open("w", newline="", encoding="utf-8")
            timestamp_writer = csv.DictWriter(
                timestamp_file,
                fieldnames=["frame_index", "timestamp", "monotonic"],
            )
            timestamp_writer.writeheader()

        parser = DebugParser()
        frame_index = 0
        with serial.Serial(args.port, args.baud, timeout=0.1) as radar:
            switch_mode(radar, 0x00)
            radar.reset_input_buffer()
            print("Debug mode enabled.")
            try:
                while not stopped.is_set():
                    chunk = radar.read(radar.in_waiting or 1)
                    if not chunk:
                        continue
                    for frame in parser.feed(chunk):
                        if raw_file:
                            raw_file.write(frame.raw)
                            raw_file.flush()
                        if timestamp_writer:
                            timestamp_writer.writerow(
                                {
                                    "frame_index": frame_index,
                                    "timestamp": f"{frame.timestamp:.9f}",
                                    "monotonic": f"{frame.monotonic:.9f}",
                                }
                            )
                            timestamp_file.flush()
                        frames.put(frame)
                        frame_index += 1
            finally:
                print("Restoring ordinary running mode...")
                switch_mode(radar, 0x64)
                print("Ordinary running mode restored.")
    except Exception as exc:
        errors.put(exc)
        stopped.set()
    finally:
        if raw_file:
            raw_file.close()
        if timestamp_file:
            timestamp_file.close()


def mirror_bin(bin_index: int) -> int:
    mirror = (2 * DEBUG_CENTER_BIN) - bin_index
    return max(0, min(DEBUG_VALUE_COUNT - 1, mirror))


def main() -> int:
    args = parse_args()
    try:
        import matplotlib.pyplot as plt
        import numpy as np
        from matplotlib.animation import FuncAnimation
    except ImportError as exc:
        raise SystemExit("Debug plotting requires matplotlib and numpy") from exc

    frames: queue.Queue = queue.Queue()
    errors: queue.Queue = queue.Queue()
    stopped = threading.Event()
    worker = threading.Thread(
        target=capture_worker,
        args=(args, frames, stopped, errors),
        daemon=False,
    )

    selected_bin = [args.bin]
    records = deque()
    latest = [None]
    estimated_rate = 4.77
    max_records = max(10, int(args.window * estimated_rate * 1.5))

    figure, axes = plt.subplots(3, 1, figsize=(12, 9))
    heat_axis, profile_axis, history_axis = axes
    figure.canvas.manager.set_window_title("S3KM1110 Debug monitor")

    heat = heat_axis.imshow(
        np.zeros((DEBUG_VALUE_COUNT, 2)),
        origin="lower",
        aspect="auto",
        extent=[-args.window, 0, 0, DEBUG_VALUE_COUNT - 1],
        vmin=0,
        vmax=5,
    )
    heat_axis.axhline(DEBUG_CENTER_BIN, color="white", linewidth=0.7, alpha=0.5)
    heat_axis.set_ylabel("Debug bin")
    heat_axis.set_title("Rolling profile heatmap — log10(value + 1)")

    bins = np.arange(DEBUG_VALUE_COUNT)
    (profile_line,) = profile_axis.plot(bins, np.ones(DEBUG_VALUE_COUNT))
    selected_marker = profile_axis.axvline(selected_bin[0], linewidth=1.2)
    mirror_marker = profile_axis.axvline(mirror_bin(selected_bin[0]), linewidth=1.0, alpha=0.65)
    profile_axis.set_yscale("log")
    profile_axis.set_xlim(0, DEBUG_VALUE_COUNT - 1)
    profile_axis.set_ylim(1, 1_000_000)
    profile_axis.set_ylabel("Value (log scale)")
    profile_axis.set_title("Current 320-bin profile — click to select")

    (selected_line,) = history_axis.plot([], [], label="selected bin")
    (mirror_line,) = history_axis.plot([], [], label="mirror bin", alpha=0.75)
    (combined_line,) = history_axis.plot([], [], label="sum", linewidth=1.7)
    history_axis.set_xlim(-args.window, 0)
    history_axis.set_ylim(0, 1)
    history_axis.set_xlabel("Seconds before now")
    history_axis.set_ylabel("Value")
    history_axis.legend(loc="upper left", ncols=3)
    status = figure.suptitle("Waiting for Debug frames...")
    figure.tight_layout(rect=(0, 0, 1, 0.96))

    def choose_bin(event) -> None:
        if event.inaxes is not profile_axis or event.xdata is None:
            return
        chosen = max(0, min(DEBUG_VALUE_COUNT - 1, round(event.xdata)))
        selected_bin[0] = chosen
        selected_marker.set_xdata([chosen, chosen])
        mirrored = mirror_bin(chosen)
        mirror_marker.set_xdata([mirrored, mirrored])

    figure.canvas.mpl_connect("button_press_event", choose_bin)

    def request_shutdown(_signal_number=None, _frame=None) -> None:
        if stopped.is_set():
            return
        print("Shutdown requested...")
        stopped.set()
        plt.close(figure)

    signal.signal(signal.SIGINT, request_shutdown)
    figure.canvas.mpl_connect("close_event", lambda _event: stopped.set())

    def update(_frame):
        if not errors.empty():
            status.set_text(f"Capture error: {errors.get_nowait()}")
            return ()

        changed = False
        while not frames.empty():
            frame = frames.get_nowait()
            latest[0] = frame
            records.append(frame)
            changed = True
        while len(records) > max_records:
            records.popleft()
        if not changed or latest[0] is None:
            return ()

        now = records[-1].monotonic
        while records and now - records[0].monotonic > args.window:
            records.popleft()
        matrix = np.asarray([record.values for record in records], dtype=float)
        relative = np.asarray([record.monotonic - now for record in records])

        heat.set_data(np.log10(matrix.T + 1))
        heat_left = relative[0] if relative[0] < 0 else -(1 / estimated_rate)
        heat.set_extent([heat_left, 0, 0, DEBUG_VALUE_COUNT - 1])
        current = np.maximum(1, matrix[-1])
        profile_line.set_ydata(current)

        chosen = selected_bin[0]
        mirrored = mirror_bin(chosen)
        selected_values = matrix[:, chosen]
        mirror_values = matrix[:, mirrored]
        combined_values = selected_values + mirror_values
        selected_line.set_data(relative, selected_values)
        mirror_line.set_data(relative, mirror_values)
        combined_line.set_data(relative, combined_values)
        maximum = max(1, float(combined_values.max()))
        history_axis.set_ylim(0, maximum * 1.1)
        status.set_text(
            f"Frames {len(records)}  |  Bin {chosen}: {int(selected_values[-1])}  |  "
            f"Mirror {mirrored}: {int(mirror_values[-1])}"
        )
        return heat, profile_line, selected_line, mirror_line, combined_line, status

    animation = FuncAnimation(
        figure, update, interval=150, blit=False, cache_frame_data=False
    )
    worker.start()
    try:
        plt.show()
    except KeyboardInterrupt:
        request_shutdown()
    finally:
        stopped.set()
        worker.join(timeout=5.0)
        if worker.is_alive():
            print("Warning: serial worker did not finish cleanup before timeout.")
    _ = animation
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
