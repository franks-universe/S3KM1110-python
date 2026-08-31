#!/usr/bin/env python3
"""Capture, decode, and replay S3KM1110 ASCII UART reports."""

from __future__ import annotations

import argparse
import csv
import json
import re
import struct
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator, TextIO


RANGE_LINE = re.compile(rb"Range\s+([0-9]+)\Z")


@dataclass(frozen=True)
class Report:
    timestamp: float
    presence: bool
    range_value: int | None
    gate_energies: tuple[int, ...]
    raw_lines: tuple[str, ...]


class S3KM1110Parser:
    """Incrementally parse ON/OFF and Range reports from arbitrary chunks."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._pending_presence: bool | None = None
        self._pending_lines: list[str] = []
        self.invalid_lines = 0

    def feed(self, data: bytes, timestamp: float | None = None) -> list[Report]:
        self._buffer.extend(data)
        reports: list[Report] = []
        stamp = time.time() if timestamp is None else timestamp

        while b"\n" in self._buffer:
            raw_line, _, remainder = self._buffer.partition(b"\n")
            self._buffer = bytearray(remainder)
            line = raw_line.rstrip(b"\r").strip()
            if not line:
                continue

            if line == b"ON":
                self._pending_presence = True
                self._pending_lines = ["ON"]
                continue

            if line == b"OFF":
                reports.append(Report(stamp, False, None, (), ("OFF",)))
                self._pending_presence = None
                self._pending_lines = []
                continue

            match = RANGE_LINE.fullmatch(line)
            if match and self._pending_presence is True:
                text = line.decode("ascii")
                reports.append(
                    Report(stamp, True, int(match.group(1)), (), tuple(self._pending_lines + [text]))
                )
                self._pending_presence = None
                self._pending_lines = []
                continue

            self.invalid_lines += 1

        return reports


REPORT_HEADER = bytes.fromhex("F4 F3 F2 F1")
REPORT_TAIL = bytes.fromhex("F8 F7 F6 F5")


class BinaryReportParser:
    """Parse 35-byte S3KM1110 binary report payloads."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.invalid_frames = 0

    def feed(self, data: bytes, timestamp: float | None = None) -> list[Report]:
        self._buffer.extend(data)
        reports: list[Report] = []
        stamp = time.time() if timestamp is None else timestamp

        while True:
            start = self._buffer.find(REPORT_HEADER)
            if start < 0:
                keep = min(len(self._buffer), 3)
                if keep:
                    del self._buffer[:-keep]
                else:
                    self._buffer.clear()
                break
            if start:
                del self._buffer[:start]
            if len(self._buffer) < 6:
                break

            payload_length = int.from_bytes(self._buffer[4:6], "little")
            frame_length = 10 + payload_length
            if payload_length != 35:
                self.invalid_frames += 1
                del self._buffer[0]
                continue
            if len(self._buffer) < frame_length:
                break

            frame = bytes(self._buffer[:frame_length])
            if frame[-4:] != REPORT_TAIL:
                self.invalid_frames += 1
                del self._buffer[0]
                continue
            del self._buffer[:frame_length]

            payload = frame[6:-4]
            presence = payload[0] == 1
            distance_cm = int.from_bytes(payload[1:3], "little")
            gates = struct.unpack("<16H", payload[3:35])
            reports.append(Report(stamp, presence, distance_cm, gates, ()))

        return reports


COMMAND_HEADER = bytes.fromhex("FD FC FB FA")
COMMAND_TAIL = bytes.fromhex("04 03 02 01")


def build_command(command_id: int, payload: bytes = b"") -> bytes:
    body = struct.pack("<H", command_id) + payload
    return COMMAND_HEADER + struct.pack("<H", len(body)) + body + COMMAND_TAIL


def switch_mode(connection, mode: int) -> None:
    packets = (
        build_command(0x00FF, struct.pack("<H", 1)),
        build_command(0x0012, struct.pack("<HI", 0, mode)),
        build_command(0x00FE),
    )
    for packet in packets:
        connection.write(packet)
        connection.flush()
        time.sleep(0.15)
        connection.read(connection.in_waiting)


def iter_serial(port: str, baud: int, raw_output: BinaryIO | None, mode: str) -> Iterator[bytes]:
    try:
        import serial
    except ImportError as exc:
        raise SystemExit("Live capture requires pyserial: python -m pip install pyserial") from exc

    with serial.Serial(port, baud, timeout=0.1) as connection:
        if mode == "report":
            switch_mode(connection, 0x04)
            connection.reset_input_buffer()
        try:
            while True:
                chunk = connection.read(connection.in_waiting or 1)
                if not chunk:
                    continue
                if raw_output:
                    raw_output.write(chunk)
                    raw_output.flush()
                yield chunk
        finally:
            if mode == "report":
                switch_mode(connection, 0x64)


def iter_file(path: Path, chunk_size: int = 4096) -> Iterator[bytes]:
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            yield chunk


class ReportWriter:
    def __init__(self, csv_file: TextIO | None, jsonl_file: TextIO | None) -> None:
        self.csv_file = csv_file
        self.jsonl_file = jsonl_file
        self.csv_writer = None
        if csv_file:
            self.csv_writer = csv.DictWriter(
                csv_file,
                fieldnames=["timestamp", "presence", "range_value"]
                + [f"gate_{index}" for index in range(16)],
            )
            self.csv_writer.writeheader()

    def write(self, report: Report) -> None:
        if self.csv_writer:
            row = {
                    "timestamp": f"{report.timestamp:.6f}",
                    "presence": int(report.presence),
                    "range_value": "" if report.range_value is None else report.range_value,
                }
            row.update({f"gate_{i}": value for i, value in enumerate(report.gate_energies)})
            self.csv_writer.writerow(row)
            self.csv_file.flush()

        if self.jsonl_file:
            self.jsonl_file.write(json.dumps(asdict(report)) + "\n")
            self.jsonl_file.flush()

        stamp = time.strftime("%H:%M:%S", time.localtime(report.timestamp))
        milliseconds = int((report.timestamp % 1) * 1000)
        if report.presence:
            suffix = ""
            if report.gate_energies:
                suffix = " gates=[" + ", ".join(map(str, report.gate_energies)) + "]"
            print(f"{stamp}.{milliseconds:03d} presence=ON range={report.range_value}{suffix}")
        else:
            print(f"{stamp}.{milliseconds:03d} presence=OFF")


def open_optional_text(path: str | None):
    if not path:
        return None
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    return output.open("w", newline="", encoding="utf-8")


def build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    live = commands.add_parser("live", help="capture and decode a serial port")
    live.add_argument("--port", required=True, help="serial port, such as COM11")
    live.add_argument("--baud", type=int, default=115200)
    live.add_argument("--mode", choices=("running", "report"), default="running")
    live.add_argument("--raw", help="write unmodified UART bytes to this file")
    live.add_argument(
        "--append", action="store_true", help="append to --raw instead of replacing it"
    )
    live.add_argument("--csv", help="write decoded reports to CSV")
    live.add_argument("--jsonl", help="write decoded reports as JSON Lines")

    replay = commands.add_parser("replay", help="decode a saved raw capture")
    replay.add_argument("input", type=Path)
    replay.add_argument("--mode", choices=("running", "report"), default="running")
    replay.add_argument("--csv", help="write decoded reports to CSV")
    replay.add_argument("--jsonl", help="write decoded reports as JSON Lines")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_cli().parse_args(argv)
    csv_file = open_optional_text(args.csv)
    jsonl_file = open_optional_text(args.jsonl)
    raw_file = None
    parser = BinaryReportParser() if args.mode == "report" else S3KM1110Parser()
    count = 0

    try:
        writer = ReportWriter(csv_file, jsonl_file)
        if args.command == "live":
            if args.raw:
                raw_path = Path(args.raw)
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_file = raw_path.open("ab" if args.append else "wb")
            print(f"Reading {args.port} at {args.baud} baud. Press Ctrl+C to stop.")
            chunks = iter_serial(args.port, args.baud, raw_file, args.mode)
        else:
            chunks = iter_file(args.input)

        for chunk in chunks:
            for report in parser.feed(chunk):
                writer.write(report)
                count += 1
    except KeyboardInterrupt:
        print("\nCapture stopped.")
        return 0
    finally:
        for handle in (raw_file, csv_file, jsonl_file):
            if handle:
                handle.close()

    invalid = getattr(parser, "invalid_lines", getattr(parser, "invalid_frames", 0))
    print(f"Decoded {count} reports; invalid records: {invalid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
