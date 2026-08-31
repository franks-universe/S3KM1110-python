#!/usr/bin/env python3
"""Live S3KM1110 gate-energy dashboard."""

from __future__ import annotations

import argparse
import csv
import queue
import signal
import threading
import time
from collections import deque
from pathlib import Path

from s3km1110_tool import BinaryReportParser, switch_mode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="serial port, such as COM11")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--gate", type=int, choices=range(16), default=3)
    parser.add_argument("--window", type=float, default=30.0, help="rolling seconds")
    parser.add_argument("--raw", type=Path, help="write unmodified report bytes")
    parser.add_argument(
        "--append", action="store_true", help="append to --raw instead of replacing it"
    )
    parser.add_argument("--csv", type=Path, help="write timestamped decoded reports")
    return parser.parse_args()


def capture_worker(args, reports, stopped, errors) -> None:
    try:
        import serial

        raw_file = None
        csv_file = None
        csv_writer = None
        if args.raw:
            args.raw.parent.mkdir(parents=True, exist_ok=True)
            raw_file = args.raw.open("ab" if args.append else "wb")
        if args.csv:
            args.csv.parent.mkdir(parents=True, exist_ok=True)
            csv_file = args.csv.open("w", newline="", encoding="utf-8")
            csv_writer = csv.DictWriter(
                csv_file,
                fieldnames=["timestamp", "monotonic", "presence", "distance_cm"]
                + [f"gate_{index}" for index in range(16)],
            )
            csv_writer.writeheader()

        parser = BinaryReportParser()
        try:
            with serial.Serial(args.port, args.baud, timeout=0.1) as radar:
                switch_mode(radar, 0x04)
                radar.reset_input_buffer()
                print("Binary report mode enabled.")
                try:
                    while not stopped.is_set():
                        chunk = radar.read(radar.in_waiting or 1)
                        if not chunk:
                            continue
                        if raw_file:
                            raw_file.write(chunk)
                            raw_file.flush()
                        for report in parser.feed(chunk):
                            received_monotonic = time.monotonic()
                            if csv_writer:
                                row = {
                                    "timestamp": f"{report.timestamp:.9f}",
                                    "monotonic": f"{received_monotonic:.9f}",
                                    "presence": int(report.presence),
                                    "distance_cm": report.range_value,
                                }
                                row.update(
                                    {
                                        f"gate_{index}": energy
                                        for index, energy in enumerate(report.gate_energies)
                                    }
                                )
                                csv_writer.writerow(row)
                                csv_file.flush()
                            reports.put(report)
                finally:
                    print("Restoring ordinary running mode...")
                    switch_mode(radar, 0x64)
                    print("Ordinary running mode restored.")
        finally:
            if raw_file:
                raw_file.close()
            if csv_file:
                csv_file.close()
    except Exception as exc:  # surfaced in the GUI thread
        errors.put(exc)
        stopped.set()


def main() -> int:
    args = parse_args()

    try:
        import matplotlib.pyplot as plt
        from matplotlib.animation import FuncAnimation
    except ImportError as exc:
        raise SystemExit(
            "Live plotting requires matplotlib: python -m pip install matplotlib"
        ) from exc

    reports: queue.Queue = queue.Queue()
    errors: queue.Queue = queue.Queue()
    stopped = threading.Event()
    worker = threading.Thread(
        target=capture_worker,
        args=(args, reports, stopped, errors),
        daemon=False,
    )

    selected_gate = [args.gate]
    times = deque()
    values = deque()
    latest = [None]

    figure, (gate_axis, history_axis) = plt.subplots(2, 1, figsize=(11, 7))
    figure.canvas.manager.set_window_title("S3KM1110 gate monitor")
    bars = gate_axis.bar(range(16), [1] * 16)
    gate_axis.set_yscale("log")
    gate_axis.set_ylim(1, 65535)
    gate_axis.set_xticks(range(16))
    gate_axis.set_xlabel("Distance gate")
    gate_axis.set_ylabel("Energy (log scale)")
    gate_axis.set_title("Current gate energy — click a bar to select")

    (history_line,) = history_axis.plot([], [], linewidth=1.5)
    history_axis.set_xlim(-args.window, 0)
    history_axis.set_ylim(0, 1)
    history_axis.set_xlabel("Seconds before now")
    history_axis.set_ylabel("Energy")
    status = figure.suptitle("Waiting for binary reports...")
    figure.tight_layout(rect=(0, 0, 1, 0.95))

    def choose_gate(event) -> None:
        if event.inaxes is not gate_axis or event.xdata is None:
            return
        gate = round(event.xdata)
        if 0 <= gate < 16:
            selected_gate[0] = gate
            times.clear()
            values.clear()

    figure.canvas.mpl_connect("button_press_event", choose_gate)

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
            error = errors.get_nowait()
            status.set_text(f"Capture error: {error}")
            return bars

        changed = False
        while not reports.empty():
            report = reports.get_nowait()
            latest[0] = report
            now = time.monotonic()
            times.append(now)
            values.append(report.gate_energies[selected_gate[0]])
            changed = True

        if not changed or latest[0] is None:
            return bars

        now = times[-1]
        while times and now - times[0] > args.window:
            times.popleft()
            values.popleft()

        report = latest[0]
        for index, (bar, energy) in enumerate(zip(bars, report.gate_energies)):
            bar.set_height(max(1, energy))
            bar.set_alpha(1.0 if index == selected_gate[0] else 0.55)

        relative_times = [stamp - now for stamp in times]
        history_line.set_data(relative_times, list(values))
        maximum = max(values, default=1)
        history_axis.set_ylim(0, max(1, maximum * 1.1))
        history_axis.set_title(f"Gate {selected_gate[0]} rolling energy")
        state = "ON" if report.presence else "OFF"
        status.set_text(
            f"Presence {state}  |  Distance {report.range_value} cm  |  "
            f"Selected gate {selected_gate[0]}: "
            f"{report.gate_energies[selected_gate[0]]}"
        )
        return (*bars, history_line, status)

    animation = FuncAnimation(figure, update, interval=100, blit=False, cache_frame_data=False)
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
