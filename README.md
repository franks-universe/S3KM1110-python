# S3KM1110 Python tools

Python tools for capturing, decoding, replaying, and visualizing UART output from
the S3KM1110 human-presence radar module.

This project documents three observed output modes:

| Mode | Value | Observed output |
| --- | ---: | --- |
| Debug | `0x00` | 320 little-endian unsigned 32-bit values per frame |
| Report | `0x04` | Presence, distance, and 16 gate-energy values |
| Running | `0x64` | ASCII `ON`/`OFF` and `Range` reports |

The Debug data resembles a centered range/FFT magnitude profile, but its exact
physical meaning and scaling have not been confirmed by the manufacturer. Do
not treat it as calibrated distance, raw ADC data, phase, or I/Q samples.

## Hardware used during development

- S3KM1110 module on a Waveshare carrier
- CP2102 USB-to-UART adapter
- 115200 baud, 8 data bits, no parity, 1 stop bit

The CP2102 is not required. Any compatible USB-UART adapter should work if its
logic voltage and wiring are appropriate for the module. The operating system
assigns the serial-port name dynamically; examples below use `COM11` only
because that was the port assigned to the development adapter.

Connect the module and adapter according to their documented voltage and UART
pinouts. At minimum, the module TX and adapter RX must share a common ground.
Bidirectional mode switching also requires adapter TX to module RX.

## Installation

Python 3.11 or newer is recommended.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest -v
```

On Linux or macOS, replace `.\.venv\Scripts\python.exe` with
`.venv/bin/python` and use the serial device assigned by the operating system.

## Running-mode capture

The default running stream is human-readable ASCII:

```powershell
.\.venv\Scripts\python.exe .\s3km1110_tool.py live `
  --port COM11 `
  --mode running `
  --raw .\captures\running.bin `
  --csv .\captures\running.csv
```

## Binary report mode

Report mode exposes presence, range, and 16 gate-energy values:

```powershell
.\.venv\Scripts\python.exe .\s3km1110_plot.py `
  --port COM11 `
  --gate 3 `
  --window 30 `
  --raw .\captures\report.bin `
  --csv .\captures\report.csv
```

Click a gate in the upper chart to display its rolling history.

## Debug mode

Debug mode exposes a 320-value profile at approximately 4.7 frames per second:

```powershell
.\.venv\Scripts\python.exe .\s3km1110_debug_plot.py `
  --port COM11 `
  --bin 176 `
  --window 60 `
  --raw .\captures\debug.bin `
  --timestamps .\captures\debug-timestamps.csv
```

The viewer shows a rolling heatmap, the current profile, and the history of a
selected bin and its mirror around the observed center bin 160. Click the
current-profile chart to select another bin.

Closing the graph or pressing `Ctrl+C` requests a clean shutdown and restores
ordinary running mode. If the process is forcibly terminated or the adapter is
disconnected, power-cycle the module or run a mode-switching tool again.

Raw outputs are replaced by default so timestamp files describe the same run.
Use `--append` only when deliberately concatenating captures. Captures and
exports are excluded from Git because they may contain experiment or participant
data.

## Offline replay

Running- and Report-mode recordings can be decoded again without hardware:

```powershell
.\.venv\Scripts\python.exe .\s3km1110_tool.py replay `
  .\captures\report.bin `
  --mode report `
  --csv .\captures\replayed.csv
```

## Observed frame formats

### Report mode

- Header: `F4 F3 F2 F1`
- Payload length: 16-bit little-endian (`35` observed)
- Presence: 1 byte
- Distance: 16-bit little-endian, observed as centimetres
- Gate energy: 16 × 16-bit little-endian
- Tail: `F8 F7 F6 F5`

### Debug mode

- Header: `AA BF 10 14`
- Values: 320 × unsigned 32-bit little-endian
- Tail: `FD FC FB FA`
- Total frame size: 1,288 bytes

The mode-changing command packets and formats in this repository were derived
from observed module behavior. Firmware variants may behave differently.

## Scope

These tools are intended for research and experimentation. They do not make the
module a medical device, and their output must not be used for diagnosis or
safety-critical monitoring without independent validation.

## License

MIT License. See [LICENSE](LICENSE).
