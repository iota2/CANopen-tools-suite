#!/usr/bin/env python3
"""
iota2 - Making Imaginations, Real
<i2.iotasquare@gmail.com>

 ██╗ ██████╗ ████████╗ █████╗ ██████╗
 ██║██╔═══██╗╚══██╔══╝██╔══██╗╚════██╗
 ██║██║   ██║   ██║   ███████║ █████╔╝
 ██║██║   ██║   ██║   ██╔══██║██╔═══╝
 ██║╚██████╔╝   ██║   ██║  ██║███████╗
 ╚═╝ ╚═════╝    ╚═╝   ╚═╝  ╚═╝╚══════╝

CANopen Monitor — Monitor data for a CAN node
=============================================

Node monitor aligned with EDS content:

Features:
 - Show raw received SDO and PDO.
 - Display node data from passed EDS file.
 - Send SDOs.

Usage examples:
---------------
# Run to display node information
python canopen_node_monitor_cli.py --interface vcan0 --local-id LOCAL_NODE_ID --local-eds LOCAL_EDS --remote-id REMOTE_NODE_ID --remote-eds REMOTE_EDS

# Run to display node information and export to CSV FileNotFoundError
python canopen_node_monitor_cli.py --interface vcan0 --local-id LOCAL_NODE_ID --local-eds LOCAL_EDS --remote-id REMOTE_NODE_ID --remote-eds REMOTE_EDS --export
"""

import can
import canopen
import threading
import time
import csv
from datetime import datetime
from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
import configparser
import struct
import sys
import queue
import argparse
import logging

# sudo ip link set can0 type can bitrate 100000000
# sudo ip link set can0 up
# cansend vcan0 181#7B0C584500000000    : PDO - 3456.78
# cansend vcan0 180#25529A442C521A46    : PDO - 1234.567 | 9876.543
# cansend vcan0 581#431A6000EFCDAB89    : SDO - 0x601A:00 - 0x89ABCDEF

class ODVariableMapper:
    def __init__(self, sdo, csv_file):
        self._sdo = sdo
        self._var_map = {}
        self._var_values = {}
        self.csv_file = csv_file
        self.last_can = ""
        self.last_sdo = ""
        self.last_pdo = ""

        for index, entry in sdo.items():
            if index >= 0x6000:
                try:
                    if hasattr(entry, "__getitem__"):
                        for subidx, subentry in entry.items():
                            name = subentry.name.replace(" ", "_")
                            varname = f"{name}_{index:04X}_{subidx}"
                            self._var_map[varname] = (index, subidx)
                            self._var_values[varname] = (None, None)
                    else:
                        name = entry.name.replace(" ", "_")
                        varname = f"{name}_{index:04X}_0"
                        self._var_map[varname] = (index, 0)
                        self._var_values[varname] = (None, None)
                except Exception as e:
                    logging.info(f"Warning parsing 0x{index:04X}: {e}")

    def log_od_change(self, var_name, value, raw_data):
        if not self.csv_file:
            return
        try:
            # Check if file already exists and has content
            with open(self.csv_file, "r") as f:
                has_header = f.readline().startswith("Time,OD Variable,Data,Raw")
        except FileNotFoundError:
            has_header = False

        with open(self.csv_file, 'a', newline='') as csvfile:
            writer = csv.writer(csvfile)
            if not has_header:
                writer.writerow(["Time", "OD Variable", "Data", "Raw"])
            raw_hex = ' '.join(f'{b:02X}' for b in raw_data) if raw_data else ''
            writer.writerow([datetime.now(), var_name, value, raw_hex])

    def update_value(self, index, subindex, value, raw_data):
        for varname, (idx, subidx) in self._var_map.items():
            if idx == index and subidx == subindex:
                old_value = self._var_values[varname]
                self._var_values[varname] = (value, raw_data)
                if old_value != (value, raw_data):
                    self.log_od_change(varname, value, raw_data)
                break

    def render_tables_split(self):
        """Render OD variables split into two side-by-side tables, with live values."""
        items = sorted(self._var_map.items(), key=lambda item: (item[1][0], item[1][1]))
        mid = (len(items) + 1) // 2
        left_items, right_items = items[:mid], items[mid:]

        def make_table(title, subset):
            # remove bold frame/title styling by not using title
            t = Table(expand=True, show_header=True, header_style="", style="magenta")
            t.add_column("OD Variable", justify="left", no_wrap=True)
            t.add_column("Hex", justify="left", no_wrap=True)
            t.add_column("Decimal", justify="right", no_wrap=True)

            for name, (idx, subidx) in subset:
                value, raw = self._var_values.get(name, (None, None))
                if raw:
                    hex_str = " ".join(f"{b:02X}" for b in raw)
                    t.add_row(name, hex_str, str(value))
                else:
                    t.add_row(name, "[dim]<not received>[/]", "[dim]—[/]")

            return t

        left_table = make_table("", left_items)
        right_table = make_table("", right_items)

        # Log actual widths for debugging using console.measure
        try:
            from rich.console import Console
            import logging
            console = Console()
            left_width = console.measure(left_table).maximum
            right_width = console.measure(right_table).maximum
            logging.debug("OD tables width - left: %s, right: %s (combined: %s)",
                          left_width, right_width, left_width + right_width)
        except Exception as e:
            import logging
            logging.error("Error measuring OD table widths: %s", e)

        return left_table, right_table

    def status_panel(self):
        return Panel.fit(
            f"[green]CAN:  {self.last_can}[/]\n"
            f"[cyan]SDO:  {self.last_sdo}[/]\n"
            f"[magenta]PDO: {self.last_pdo}[/]",
            title="Live Status"
        )


class CommandInput:
    def __init__(self):
        self.current_input = ""
        self.history = []
        self.prompt = ">>> "
        self.output_lines = []
        self.max_lines = 5
        self.pending_commands = queue.Queue()

    def render_cli(self):
        lines = self.output_lines[-self.max_lines:] + [self.prompt + self.current_input]
        content = "\n".join(lines)
        return Panel(content, title="Command", border_style="cyan", expand=True, width=80)

    def append_output(self, line):
        self.output_lines.append(line)

    def feed_key(self, key: str):
        if key == "\r" or key == "\n":
            self.output_lines.append(self.prompt + self.current_input)
            self.history.append(self.current_input)
            self.pending_commands.put(self.current_input.strip())
            self.current_input = ""
        elif key == "\x7f":  # Backspace
            self.current_input = self.current_input[:-1]
        else:
            self.current_input += key

    def get_next_command(self):
        try:
            return self.pending_commands.get_nowait()
        except queue.Empty:
            return None


class CanopenMonitor:
    def __init__(self, can_interface, local_node_id, local_eds_file, remote_node_id, remote_eds_file, csv_file):
        self.console = Console()
        self.network = canopen.Network()
        self.local_node_id = local_node_id
        self.local_eds_file = local_eds_file
        self.remote_node_id = remote_node_id
        self.remote_eds_file = remote_eds_file
        self.csv_file = csv_file
        self.command_queue = queue.Queue()
        self.cmd_input = CommandInput()

        # store interface so listener uses the same one passed via CLI
        self.can_interface = can_interface

        self.network.connect(channel=can_interface, interface="socketcan")
        # Local node acts like a device on the bus
        self.node_local = canopen.LocalNode(self.local_node_id, self.local_eds_file)
        self.network.add_node(self.node_local)
        self.node_remote = canopen.RemoteNode(self.remote_node_id, self.remote_eds_file)
        self.network.add_node(self.node_remote)
        self.od_vars = ODVariableMapper(self.node_local.sdo, self.csv_file)

        self.setup_pdos()
        logging.info("Monitor initialized on interface %s", self.can_interface)

    def setup_pdos(self):
        self._setup_rpdos()
        self._setup_tpdos()

    def _parse_pdo(self, section_base, map_base, limit):
        config = configparser.ConfigParser(strict=False)
        config.optionxform = str
        config.read(self.local_eds_file)

        pdos = []
        for num in range(limit):
            comm = f"{section_base}{num}sub1"
            map_prefix = f"{map_base}{num}sub"
            try:
                raw_val = config[comm]["DefaultValue"].split(';')[0].strip()
                cob_id = int(raw_val, 0)
            except KeyError:
                break

            mapping = []
            subidx = 1
            while f"{map_prefix}{subidx}" in config:
                raw_entry = config[f"{map_prefix}{subidx}"]["DefaultValue"].split(';')[0].strip()
                raw = int(raw_entry, 0)
                index = (raw >> 16) & 0xFFFF
                sub = (raw >> 8) & 0xFF
                size = raw & 0xFF
                mapping.append((index, sub, size))
                subidx += 1
            pdos.append((cob_id, mapping))
        return pdos

    def _setup_rpdos(self):
        pdos = self._parse_pdo("140", "160", 512)
        for i, (cob_id, mapping) in enumerate(pdos, 1):
            pdo = self.node_local.pdo.rx[i]
            pdo.clear()
            pdo.cob_id = cob_id
            for index, subidx, size in mapping:
                if index == 0x0000:
                    continue  # skip unused mapping
                pdo.add_variable(index, subidx, size)
            pdo.enabled = True

    def _setup_tpdos(self):
        pdos = self._parse_pdo("180", "1A0", 512)
        for i, (cob_id, mapping) in enumerate(pdos, 1):
            pdo = self.node_local.pdo.tx[i]
            pdo.clear()
            pdo.cob_id = cob_id
            for index, subidx, size in mapping:
                if index == 0x0000:
                    continue  # skip unused mapping
                pdo.add_variable(index, subidx, size)
            pdo.enabled = True

    def handle_can(self, msg):
        self.od_vars.last_can = f"{msg.arbitration_id:03X} [{msg.dlc}]: {' '.join(f'{b:02X}' for b in msg.data)}"
        logging.debug("CAN frame: %s", self.od_vars.last_can)
        if 0x180 <= msg.arbitration_id <= 0x4FF:
            self._handle_pdo(msg)
        # handle SDO request (0x600+node) and SDO response (0x580+node)
        elif (0x580 <= msg.arbitration_id <= 0x5FF) or (0x600 <= msg.arbitration_id <= 0x67F):
            self._handle_sdo(msg)

    def _handle_sdo(self, msg):
        # Accept SDOs both from 0x580..0x5FF (server->client) and 0x600..0x67F (client->server)
        # Parse index/subindex from bytes 1..3 and consider bytes 4..end as payload (if any).
        logging.debug("Handling SDO: COB-ID=0x%03X data=%s", msg.arbitration_id,
                        " ".join(f"{b:02X}" for b in msg.data))
        if len(msg.data) < 4:
            # Not a valid SDO frame we can parse
            self.od_vars.last_sdo = f"SDO: <invalid:{msg.arbitration_id:03X}>"
            logging.warning("Invalid SDO frame received: %s", self.od_vars.last_sdo)
            return

        cmd = msg.data[0]
        index = (msg.data[2] << 8) | msg.data[1]
        subidx = msg.data[3]
        payload = msg.data[4:]

        # If there's payload, treat it as an expedited write value (little-endian)
        if payload:
            try:
                value = int.from_bytes(payload, 'little')
            except Exception:
                value = None
            try:
                name = self.node_local.sdo[index].name
            except Exception:
                name = f"0x{index:04X}"
            raw = bytes(payload)
            if value is not None:
                self.od_vars.last_sdo = f"{name} (0x{index:04X}, {subidx}) = 0x{value:X}"
                # update OD variables if index/subindex present in ODVariableMapper
                self.od_vars.update_value(index, subidx, value, raw)
                logging.info("SDO write: %s", self.od_vars.last_sdo)
            else:
                # non-integer payload — still show raw
                hex_str = " ".join(f"{b:02X}" for b in raw)
                self.od_vars.last_sdo = f"{name} (0x{index:04X}, {subidx}) payload: {hex_str}"
                logging.info("SDO raw payload: %s", self.od_vars.last_sdo)
        else:
            # No payload — could be an SDO command/response without data (e.g. upload request)
            try:
                name = self.node_local.sdo[index].name
            except Exception:
                name = f"0x{index:04X}"
            self.od_vars.last_sdo = f"{name} (0x{index:04X}, {subidx}) cmd=0x{cmd:02X}"
            logging.info("SDO command: %s", self.od_vars.last_sdo)

    def _handle_pdo(self, msg):
        logging.debug("Handling PDO: COB-ID=0x%03X data=%s", msg.arbitration_id,
                        " ".join(f"{b:02X}" for b in msg.data))
        for pdo in self.node_local.pdo.values():
            if pdo.cob_id == msg.arbitration_id:
                try:
                    offset = 0
                    lines = []
                    for var in pdo:
                        if var.index == 0x0000:  # skip dummy entries
                            continue

                        bits = var.length or 32
                        size = (bits + 7) // 8
                        data = msg.data[offset:offset+size]
                        value = (
                            struct.unpack('<f', data)[0] if size == 4 else
                            struct.unpack('<d', data)[0] if size == 8 else
                            int.from_bytes(data, 'little')
                        )
                        self.od_vars.update_value(var.index, var.subindex, value, data)
                        lines.append(f"{var.name} = {value}")
                        offset += size
                    self.od_vars.last_pdo = " | ".join(lines)
                    logging.info("PDO update: %s", self.od_vars.last_pdo)
                    return
                except Exception as e:
                    self.od_vars.last_pdo = f"PDO Decode Error: {str(e)}"
                    logging.error("PDO decode error: %s", e)
                    return
        self.od_vars.last_pdo = f"PDO: <unmatched:{msg.arbitration_id:X}>"
        logging.warning("Unmatched PDO: COB-ID=0x%03X", msg.arbitration_id)

    def start(self):
        threading.Thread(target=self.keyboard_input_loop, daemon=True).start()
        threading.Thread(target=self.command_handler, daemon=True).start()
        threading.Thread(target=self.can_listener, daemon=True).start()

        console = Console()
        with Live(console=self.console, refresh_per_second=5, screen=True, vertical_overflow="visible") as live:
            while True:
                # build OD variable tables first
                left, right = self.od_vars.render_tables_split()
                two_tables = Table.grid(expand=True)
                two_tables.add_row(left, right)

                # measure actual widths using console.measure
                left_width = console.measure(left).maximum
                right_width = console.measure(right).maximum
                total_width = left_width + right_width

                status_panel = Panel.fit(
                    f"[green]CAN:  {self.od_vars.last_can}[/]\n"
                    f"[cyan]SDO:  {self.od_vars.last_sdo}[/]\n"
                    f"[magenta]PDO: {self.od_vars.last_pdo}[/]",
                    title="Live Status",
                    width=total_width
                )

                layout = Table.grid(padding=(0, 1))
                layout.add_row(status_panel)
                layout.add_row(two_tables)
                layout.add_row(self.cmd_input.render_cli())
                time.sleep(0.2)
                live.update(layout)

    def keyboard_input_loop(self):
        import sys, termios, tty
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while True:
                ch = sys.stdin.read(1)
                self.cmd_input.feed_key(ch)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    def command_handler(self):
        while True:
            cmd = self.cmd_input.get_next_command()
            if not cmd:
                time.sleep(0.1)
                continue
            self.cmd_input.append_output(f"Executed: {cmd}")  # placeholder

    def can_listener(self):
        # use the same CAN interface provided to the monitor (e.g. vcan0)
        bus = can.interface.Bus(channel=self.can_interface, interface="socketcan")
        while True:
            msg = bus.recv()
            if msg:
                self.handle_can(msg)


def main():
    parser = argparse.ArgumentParser(description="CANopen Node Simulator")
    parser.add_argument("--interface", default="can0", help="CAN interface (default: can0)")
    parser.add_argument("--local-id", type=lambda x: int(x, 0), default=0x01, help="Local node ID")
    parser.add_argument("--local-eds", default="u.eds", help="Local EDS file")
    parser.add_argument("--remote-id", type=lambda x: int(x, 0), default=0x02, help="Remote node ID")
    parser.add_argument("--remote-eds", default="p.eds", help="Remote EDS file")
    parser.add_argument("--export", action="store_true", help="Export OD changes to canopen_node_simulator.csv")
    # --log is a flag only (no argument). Logs are always written to ./canopen_node_monitor_cli.log
    parser.add_argument("--log", action="store_true", help="Enable extra logging (always written to canopen_node_monitor_cli.log)")

    args = parser.parse_args()

    csv_file = "canopen_node_monitor_cli.csv" if args.export else None

    # Always use this fixed log filename
    log_file = "canopen_node_monitor_cli.log"

    logging.basicConfig(
        filename=log_file,
        level=logging.DEBUG if args.log else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    logging.info("=== CANopen Node Monitor started ===")

    monitor = CanopenMonitor(
        can_interface=args.interface,
        local_node_id=args.local_id,
        local_eds_file=args.local_eds,
        remote_node_id=args.remote_id,
        remote_eds_file=args.remote_eds,
        csv_file=csv_file
    )
    monitor.start()


if __name__ == "__main__":
    main()
