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

# CANopen Node Monitor

**canopen_node_monitor_cli.py** — A live CANopen node sniffer and OD variable monitor with interactive Rich TUI.

---

## Features

- Connects to a CANopen network via **SocketCAN**
- Uses **LocalNode** and **RemoteNode** from EDS files
- Live display of:
  - Raw CAN frames
  - SDO requests/responses
  - PDO updates (decoded via EDS mapping)
- Rich TUI with:
  - Split tables of OD variables
  - Live status panel (last CAN, SDO, PDO)
  - Interactive command input panel
- CSV export of OD variable changes
- Logging to `canopen_node_monitor_cli.log`

---

## Dependencies

```bash
pip install python-can canopen rich
```

---

## Usage

### Start the monitor (default interface: `can0`)
```bash
python canopen_node_monitor_cli.py --interface vcan0 --local-eds local.eds --remote-eds remote.eds
```

### With custom node IDs
```bash
python canopen_node_monitor_cli.py --interface vcan0 --local-id 0x01 --remote-id 0x02
```

### Export OD variable changes to CSV
```bash
python canopen_node_monitor_cli.py --interface vcan0 --local-eds u.eds --remote-eds p.eds --export
```

### Enable detailed logging
```bash
python canopen_node_monitor_cli.py --interface vcan0 --log
```

### All options enabled
```bash
python canopen_node_monitor_cli.py --interface vcan0 --local-id 0x01 --local-eds local.eds --remote-id 0x01 --remote-eds remote.eds --log --export
```

---

## Command-line Options

| Option              | Description |
|---------------------|-------------|
| `--interface NAME`  | SocketCAN interface (default: `can0`) |
| `--local-id HEX`    | Local node ID (default: 0x01) |
| `--local-eds PATH`  | Local EDS file (default: `u.eds`) |
| `--remote-id HEX`   | Remote node ID (default: 0x02) |
| `--remote-eds PATH` | Remote EDS file (default: `p.eds`) |
| `--export`          | Export OD changes to `canopen_node_monitor_cli.csv` |
| `--log`             | Enable verbose logging (`canopen_node_monitor_cli.log`) |

---

## Example Workflow

1. Set up virtual CAN interface:
   ```bash
   sudo modprobe vcan
   sudo ip link add dev vcan0 type vcan
   sudo ip link set vcan0 up
   ```

2. Run the monitor:
   ```bash
   python canopen_node_monitor_cli.py --interface vcan0 --local-id 0x01 --local-eds ../eds_files/sample_device_tpdo.eds --remote-id 0x01 --remote-eds ../eds_files/sample_device_rpdo.eds --log
   ```

3. In another terminal, inject frames:
   ```bash
   cansend vcan0 181#7B0C584500000000    # PDO example
   cansend vcan0 180#25529A442C521A46    # PDO with two floats
   cansend vcan0 581#431A6000EFCDAB89    # SDO example
   ```

4. Watch decoded values update in the Rich TUI.

---

## Logging & Export

- **Logs:** if `--local` is set, written to `canopen_node_monitor_cli.log`
- **OD Changes:** if `--export` is set, written to `canopen_node_monitor_cli.csv`

---