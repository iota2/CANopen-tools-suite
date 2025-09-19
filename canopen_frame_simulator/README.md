> `iota2` - Making Imaginations, Real
>
> <i2.iotasquare@gmail.com>


```
 ██╗ ██████╗ ████████╗ █████╗ ██████╗
 ██║██╔═══██╗╚══██╔══╝██╔══██╗╚════██╗
 ██║██║   ██║   ██║   ███████║ █████╔╝
 ██║██║   ██║   ██║   ██╔══██║██╔═══╝
 ██║╚██████╔╝   ██║   ██║  ██║███████╗
 ╚═╝ ╚═════╝    ╚═╝   ╚═╝  ╚═╝╚══════╝
```

# CANopen Frame Simulator

**canopen_frame_simulator.py** — A flexible CANopen frame simulator aligned with EDS content.

---

## Features

- Parse TPDO mappings dynamically from an EDS file
- Send mapped PDOs with auto-generated float/int values
- Send only unmapped OD entries (>0x6000) as SDOs
- Send heartbeat frames (`0x700 + NodeID`) from EDS
- Optional **Time Stamp** (0x100)
- Optional **Emergency** (0x80 + NodeID)
- Fallback to demo frames if no EDS provided
- Optional logging to `simulate_can_frames.log`

---

## Dependencies

```bash
pip install python-can tqdm
```

---

## Usage

### Run with virtual CAN and fallback demo frames
```bash
python canopen_frame_simulator.py --interface vcan0 --count 10
```

### Run with EDS-defined PDO/SDO mappings and TimeStamp
```bash
python canopen_frame_simulator.py --interface vcan0 --count 50 --eds sample_device.eds --with-timestamp
```

### Run with EMCY injection
```bash
python canopen_frame_simulator.py --interface vcan0 --count 20 --eds sample_device.eds --with-emcy
```

---

## Command-line Options

| Option             | Description |
|--------------------|-------------|
| `--interface NAME` | SocketCAN interface (default: `vcan0`) |
| `--count N`        | Number of update cycles to send (default: 5) |
| `--eds PATH`       | Path to EDS file |
| `--log`            | Enable logging to `canopen_frame_simulator.log` |
| `--with-timestamp` | Send Time Stamp frames (0x100) |
| `--with-emcy`      | Send Emergency frames (0x80 + NodeID) |

---

## Example Workflow

1. Set up virtual CAN interface:
   ```bash
   sudo modprobe vcan
   sudo ip link add dev vcan0 type vcan
   sudo ip link set vcan0 up
   ```

2. Run the simulator:
   ```bash
   python canopen_frame_simulator.py --interface vcan0 --count 20 --eds sample_device.eds --with-timestamp --with-emcy
   ```

3. Observe frames using `candump` or your sniffer tool.

---

## Logging

- Enable logging with `--log`
- Output is written to `canopen_frame_simulator.log`

---