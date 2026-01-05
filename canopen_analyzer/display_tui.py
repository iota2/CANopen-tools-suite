#!/usr/bin/env python3
# ██╗ ██████╗ ████████╗ █████╗ ██████╗
# ██║██╔═══██╗╚══██╔══╝██╔══██╗╚════██╗
# ██║██║   ██║   ██║   ███████║ █████╔╝
# ██║██║   ██║   ██║   ██╔══██║██╔═══╝
# ██║╚██████╔╝   ██║   ██║  ██║███████╗
# ╚═╝ ╚═════╝    ╚═╝   ╚═╝  ╚═╝╚══════╝
# Copyright (c) 2025 iota2 (iota2 Engineering Tools)
# Licensed under the MIT License. See LICENSE file in the project root for details.

"""!
@file display_tui.py
@brief Textual-based interactive TUI frontend for CANopen monitoring.
@details
This module implements an interactive terminal user interface using the
Textual framework. It provides structured tables, rolling graphs, and
keyboard shortcuts for inspecting CANopen traffic in real time.

### Responsibilities
- Render protocol, PDO, SDO, and bus statistics tables
- Provide fixed and scrolling display modes
- Support keyboard shortcuts for copying table data
- Integrate with Textual event and rendering loops

### Design Notes
- The Textual App class is defined lazily to allow import without Textual.
- No protocol parsing or statistics computation occurs here.
- UI logic is isolated from data collection logic.

### Threading Model
The TUI runs in a blocking Textual event loop and should be launched from
the main application thread or a dedicated process.

### Error Handling
Textual availability and runtime errors are caught and logged, with optional
fallback to CLI mode.
"""

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.widgets import (
        Header, Footer, DataTable, Static,
        Switch, Input, Button, RadioSet, RadioButton
    )
    from textual import events
except Exception:
    App = None  # textual may be missing

from rich.text import Text

import copy
import pyperclip
import logging

import analyzer_defs as analyzer_defs

class display_tui:
    """! Textual-based TUI implementation for CANopen protocol.
    @details
    This class provides a blocking `run_textual(stats, processed_frame, fixed)`
    entrypoint that will start a textual App that renders the CANopen information.
    """

    # class attributes to be set by caller before run_textual()

    ## Private instance for pointing to incoming bus stats.
    stats = None

    ## Private instance for pointing to incoming processed frames.
    processed_frame = None

    ## Private instance for pointing to outgoing frames.
    requested_frame = None

    ## Private instance for pointing to incoming flag whether to keep display in fixed mode or not.
    fixed = False

    ## TUI display refresh rate (seconds)
    refresh_interval = 0.2

    @classmethod
    def run_textual(cls, stats, processed_frame=None, requested_frame=None, fixed=False):
        """! Start the Textual-based CANopen TUI.
        @details
        This method initializes and launches the Textual application that renders
        all live CANopen monitoring tables (Protocol, PDO, SDO, Bus Stats). It sets
        up shared state used by the UI update loop, including statistics, frame
        queues, and the fixed/scrolling display mode.
        @param stats The shared stats object providing real-time bus statistics and rate histories.
        @param processed_frame Queue delivering processed CANopen frames from the background sniffer thread.
        @param requested_frame Queue delivering CANopen frames to the background sniffer thread.
        @param fixed When True, tables operate in fixed-index mode; otherwise they show scrolling entries.
        @return None
        """

        # Lazy import check
        if App is None:
            raise RuntimeError("textual is not installed. Install with: pip install textual")
        # set class attrs
        cls.stats = stats
        cls.processed_frame = processed_frame
        cls.requested_frame = requested_frame
        cls.fixed = fixed

        # Define the actual App class inside this method so that the module
        # can be imported even if textual is not available.
        class tui_app(App):
            CSS_PATH = None

            BINDINGS = [
                Binding(key="q", action="quit", description="Quit the app"),
                Binding(
                    key="question_mark",
                    action="help",
                    description="Show help screen",
                    key_display="?",
                ),
                Binding(key="n", action="Copy Protocol data", description="Copy protocol table data"),
                Binding(key="b", action="Copy Bus stats", description="Copy bus stats table"),
                Binding(key="p", action="Copy PDO", description="Copy PDO table"),
                Binding(key="s", action="Copy SDO", description="Copy SDO table"),
            ]

            def __init__(self, *a, **kw):
                """! TUI interface initialization."""

                super().__init__(*a, **kw)

                ## Logger instance for TUI display.
                self.logger = logging.getLogger(self.__class__.__name__)

                ## Timer for repeating remote node control
                self._repeat_tasks = {}

                ## Protocol data dict keys -> rows mapping for fixed mode
                self.fixed_proto = {}

                ## PDO data dict keys -> rows mapping for fixed mode
                self.fixed_pdo = {}

                ## SDO data dict keys -> rows mapping for fixed mode
                self.fixed_sdo = {}

                # Display buffers (fixed-size lists) used for top-down filling in scrolling mode.
                blank_proto = {"time": "", "cob": "", "type": "", "raw": "", "decoded": "", "count": ""}
                blank_pdo = {"time": "", "cob": "", "dir": "", "name": "", "index": "", "sub": "", "raw": "", "decoded": "", "count": ""}
                blank_sdo = {"time": "", "cob": "", "dir": "","name": "", "index": "", "sub": "", "raw": "", "decoded": "", "count": ""}

                ## Protocol data display buffer
                self.proto_display = [copy.deepcopy(blank_proto) for _ in range(analyzer_defs.PROTOCOL_TABLE_HEIGHT)]

                ## PDO data display buffer
                self.pdo_display = [copy.deepcopy(blank_pdo) for _ in range(analyzer_defs.DATA_TABLE_HEIGHT)]

                ## SDO data display buffer
                self.sdo_display = [copy.deepcopy(blank_sdo) for _ in range(analyzer_defs.DATA_TABLE_HEIGHT)]

                ## Protocol data indices to indicate next fill position (top-down). When full, we roll by popping index 0.
                self.proto_next_index = 0

                ## PDO data indices to indicate next fill position (top-down). When full, we roll by popping index 0.
                self.pdo_next_index = 0

                ## SDO data indices to indicate next fill position (top-down). When full, we roll by popping index 0.
                self.sdo_next_index = 0

                # cache last bus stats textual dump for copy
                self._last_bus_stats = None

            def compose(self) -> ComposeResult:
                """! Textual compose callback."""

                # Application header
                yield Header()

                # two-column layout (left: proto + pdo, right: bus stats + sdo)
                with Horizontal():
                    with Vertical(classes="left column"):
                        yield Static("[b]Protocol Data[/b]", classes="header protocol")
                        ## TUI Element for protocol data table
                        self.proto_table = DataTable(zebra_stripes=True, show_cursor=False, classes="table protocol")
                        self.proto_table.styles.height = analyzer_defs.PROTOCOL_TABLE_HEIGHT + 1
                        yield self.proto_table

                        yield Static("")
                        yield Static("[b]PDO Data[/b]", classes="header pdo")
                        ## TUI Element for PDO data table
                        self.pdo_table = DataTable(zebra_stripes=True, show_cursor=False, classes="table pdo")
                        self.pdo_table.styles.height = analyzer_defs.DATA_TABLE_HEIGHT + 1
                        yield self.pdo_table

                    with Vertical(classes="right column"):
                        # Bus Stats now a DataTable with columns: Metric, Value, Graph
                        yield Static("[b]Bus Stats[/b]", classes="header busstats")
                        ## TUI Element for bus stats table
                        self.bus_stats_table = DataTable(zebra_stripes=True, show_cursor=False, classes="table busstats")
                        self.bus_stats_table.styles.height = analyzer_defs.PROTOCOL_TABLE_HEIGHT + 1
                        yield self.bus_stats_table

                        yield Static("")
                        ## TUI Element for SDO data table
                        yield Static("[b]SDO Data[/b]", classes="header sdo")
                        self.sdo_table = DataTable(zebra_stripes=True, show_cursor=False, classes="table sdo")
                        self.sdo_table.styles.height = analyzer_defs.DATA_TABLE_HEIGHT + 1
                        yield self.sdo_table

                # Add remote node controls
                with Vertical(classes="root remote"):
                    header = Static("[b]Remote Node Control[/b]", classes="header remote")
                    yield header

                    with Horizontal(classes="row remote"):
                        # SDO send -----------------------------------------
                        with Vertical(classes="column remote"):
                            yield Static("[b]Send SDO[/b]", classes="subheader remote send sdo")
                            yield Static("")

                            with Horizontal(classes="content remote sdo send"):
                                lbl = Input("Node ID", disabled=True, classes="content remote sdo send")
                                lbl.styles.width = 20
                                yield lbl
                                ## User input text box of entering SDO send node-id
                                self.sdo_send_node = Input(analyzer_defs.DEFAULT_SDO_SEND_NODE_ID)
                                self.sdo_send_node.styles.width = 20
                                yield self.sdo_send_node

                            with Horizontal(classes="content remote sdo send"):
                                lbl = Input("Index", disabled=True, classes="content remote sdo send")
                                lbl.styles.width = 20
                                yield lbl
                                ## User input text box of entering SDO index
                                self.sdo_send_index = Input(analyzer_defs.DEFAULT_SDO_SEND_INDEX)
                                self.sdo_send_index.styles.width = 20
                                yield self.sdo_send_index

                                lbl = Input("Sub", disabled=True, classes="content remote sdo send")
                                lbl.styles.width = 20
                                yield lbl
                                ## User input text box of entering SDO send sub-index
                                self.sdo_send_sub = Input(analyzer_defs.DEFAULT_SDO_SEND_SUB)
                                self.sdo_send_sub.styles.width = 20
                                yield self.sdo_send_sub

                            with Horizontal(classes="content remote sdo send"):
                                lbl = Input("Data", disabled=True, classes="content remote sdo send")
                                lbl.styles.width = 20
                                yield lbl
                                ## User input text box of entering SDO send data.
                                self.sdo_send_value = Input(analyzer_defs.DEFAULT_SDO_SEND_DATA)
                                self.sdo_send_value.styles.width = 20
                                yield self.sdo_send_value

                                lbl = Input("Size", disabled=True, classes="content remote sdo send")
                                lbl.styles.width = 20
                                yield lbl
                                # --- Size selector ---
                                ## Radio button selection for SDO send data size.
                                self.sdo_send_size = RadioSet(classes="radio remote")
                                with self.sdo_send_size:
                                    yield RadioButton("1", value=True)
                                    yield RadioButton("2")
                                    yield RadioButton("4")

                                self.sdo_send_size.styles.width = 20
                                yield self.sdo_send_size

                            with Horizontal(classes="content remote sdo send"):
                                lbl = Input("Repeat (ms)", disabled=True, classes="content remote sdo send")
                                lbl.styles.width = 20
                                yield lbl
                                ## User input text box of entering SDO send repeat values.
                                self.sdo_send_repeat_value = Input(analyzer_defs.DEFAULT_SDO_SEND_REPEAT_TIME)
                                self.sdo_send_repeat_value.styles.width = 20
                                yield self.sdo_send_repeat_value
                                ## SDO send repeat switch.
                                self.sdo_send_repeat = Switch()
                                yield self.sdo_send_repeat

                                # Button for SDO send.
                                self.sdo_send_btn = Button("Send", classes="button remote")
                                self.sdo_send_btn.styles.width = 29
                                yield self.sdo_send_btn

                        # SDO receive --------------------------------------
                        with Vertical(classes="column remote"):
                            yield Static("[b]Receive SDO[/b]", classes="subheader remote sdo receive")
                            yield Static("")

                            with Horizontal(classes="content remote sdo receive"):
                                lbl = Input("Node ID", disabled=True, classes="content remote sdo receive")
                                lbl.styles.width = 20
                                yield lbl
                                ## User input text box of entering SDO receive node-id.
                                self.sdo_recv_node = Input(analyzer_defs.DEFAULT_SDO_RECV_NODE_ID)
                                self.sdo_recv_node.styles.width = 20
                                yield self.sdo_recv_node

                            with Horizontal(classes="content remote sdo receive"):
                                lbl = Input("Index", disabled=True, classes="content remote sdo receive")
                                lbl.styles.width = 20
                                yield lbl
                                ## User input text box of entering SDO receive index
                                self.sdo_recv_index = Input(analyzer_defs.DEFAULT_SDO_RECV_INDEX)
                                self.sdo_recv_index.styles.width = 20
                                yield self.sdo_recv_index

                                lbl = Input("Sub", disabled=True, classes="content remote sdo receive")
                                lbl.styles.width = 20
                                yield lbl
                                ## User input text box of entering SDO receive sub-index
                                self.sdo_recv_sub = Input(analyzer_defs.DEFAULT_SDO_RECV_SUB)
                                self.sdo_recv_sub.styles.width = 20
                                yield self.sdo_recv_sub

                            with Horizontal(classes="content remote sdo receive"):
                                lbl = Input("Repeat (ms)", disabled=True, classes="content remote sdo receive")
                                lbl.styles.width = 20
                                yield lbl
                                ## User input text box of entering SDO receive repeat values.
                                self.sdo_recv_repeat_value = Input(analyzer_defs.DEFAULT_SDO_RECV_REPEAT_TIME)
                                self.sdo_recv_repeat_value.styles.width = 20
                                ## SDO receive repeat toggle switch.
                                yield self.sdo_recv_repeat_value
                                self.sdo_recv_repeat = Switch()
                                yield self.sdo_recv_repeat

                                ## Button for SDO receive
                                self.sdo_recv_btn = Button("Send", classes="button remote")
                                self.sdo_recv_btn.styles.width = 29
                                yield self.sdo_recv_btn

                        # PDO send -----------------------------------------
                        with Vertical(classes="column remote"):
                            yield Static("[b]Send PDO[/b]", classes="subheader remote pdo send")
                            yield Static("")

                            with Horizontal(classes="content remote pdo send"):
                                lbl = Input("COB ID", disabled=True, classes="content remote pdo send")
                                lbl.styles.width = 20
                                yield lbl
                                ## User input text box of entering PDO send cob-id.
                                self.pdo_cob = Input(analyzer_defs.DEFAULT_PDO_SEND_COB_ID)
                                self.pdo_cob.styles.width = 20
                                yield self.pdo_cob

                            with Horizontal(classes="content remote pdo send"):
                                lbl = Input("Data", disabled=True, classes="content remote pdo send")
                                lbl.styles.width = 20
                                yield lbl
                                ## User input text box of entering PDO send data.
                                self.pdo_data = Input(analyzer_defs.DEFAULT_PDO_SEND_DATA)
                                self.pdo_data.styles.width = 30
                                yield self.pdo_data

                            with Horizontal(classes="content remote pdo send"):
                                lbl = Input("Repeat (ms)", disabled=True, classes="content remote pdo send")
                                lbl.styles.width = 20
                                yield lbl
                                ## User input text box of entering PDO send repeat value.
                                self.pdo_send_repeat_value = Input(analyzer_defs.DEFAULT_PDO_SEND_REPEAT_TIME)
                                self.pdo_send_repeat_value.styles.width = 20
                                yield self.pdo_send_repeat_value
                                ## PDO send repeat toggle switch.
                                self.pdo_repeat = Switch()
                                yield self.pdo_repeat

                                ## Button for PDO send.
                                self.pdo_send_btn = Button("Send", classes="button remote")
                                self.pdo_send_btn.styles.width = 29
                                yield self.pdo_send_btn

                # footer with key hints
                yield Footer()

            async def on_mount(self) -> None:
                """! Textual on_mount callback"""

                self.logger.info("display_tui mounted")
                ## Title to display on TUI console.
                self.title = analyzer_defs.APP_ORG

                ## Sub title to display on TUI console.
                self.sub_title = analyzer_defs.APP_NAME

                # build DataTable columns to match Rich version (Textual DataTable doesn't accept no_wrap/key args)
                # Protocol table
                self.proto_table.clear(columns=True)
                self.proto_table.add_column("Time")
                self.proto_table.add_column("COB-ID")
                self.proto_table.add_column("Type")
                self.proto_table.add_column("Raw Data")
                self.proto_table.add_column("Decoded")
                self.proto_table.add_column("Count")

                # PDO table
                self.pdo_table.clear(columns=True)
                self.pdo_table.add_column("Time")
                self.pdo_table.add_column("COB-ID")
                self.pdo_table.add_column("Dir")
                self.pdo_table.add_column("Name")
                self.pdo_table.add_column("Index")
                self.pdo_table.add_column("Sub")
                self.pdo_table.add_column("Raw Data")
                self.pdo_table.add_column("Decoded")
                self.pdo_table.add_column("Count")

                # SDO table
                self.sdo_table.clear(columns=True)
                self.sdo_table.add_column("Time")
                self.sdo_table.add_column("COB-ID")
                self.sdo_table.add_column("Dir")
                self.sdo_table.add_column("Name")
                self.sdo_table.add_column("Index")
                self.sdo_table.add_column("Sub")
                self.sdo_table.add_column("Raw Data",)
                self.sdo_table.add_column("Decoded")
                self.sdo_table.add_column("Count")

                # Bus stats table columns: Metric, Value, Graph
                self.bus_stats_table.clear(columns=True)
                self.bus_stats_table.add_column("Metric", width=30)
                self.bus_stats_table.add_column("Value", width=50)
                self.bus_stats_table.add_column("Graph", width=analyzer_defs.STATS_GRAPH_WIDTH)

                # enforce fixed visual heights so DataTable doesn't expand indefinitely
                try:
                    # add header row height cushion (~3) and a small margin
                    self.proto_table.height = max(3, analyzer_defs.PROTOCOL_TABLE_HEIGHT)
                    self.pdo_table.height = max(3, analyzer_defs.DATA_TABLE_HEIGHT)
                    self.sdo_table.height = max(3, analyzer_defs.DATA_TABLE_HEIGHT)
                    self.bus_stats_table.height = max(6, len(["State","Active Nodes","PDO Frames/s","SDO Frames/s","HB Frames/s","EMCY Frames/s","Total Frames/s","Peak Frames/s","Bus Util %","Bus Idle %"]))
                except Exception:
                    # older textual versions may not allow setting height attribute directly; ignore gracefully
                    pass

                # schedule periodic update (poll queue + refresh stats)
                self.set_interval(cls.refresh_interval, self._update_from_queue)

                # Populate tables immediately with blank rows so the UI shows fixed-height empty tables on startup.
                try:
                    # protocol
                    for row in self.proto_display:
                        try:
                            self.proto_table.add_row(row.get("time",""), row.get("cob",""), row.get("type",""), row.get("raw",""), row.get("decoded",""), str(row.get("count","")))
                        except Exception:
                            pass
                    # pdo
                    for row in self.pdo_display:
                        try:
                            self.pdo_table.add_row(row.get("time",""), row.get("cob",""), row.get("dir", ""), row.get("name",""), row.get("index",""), row.get("sub",""), row.get("raw",""), str(row.get("decoded","")), str(row.get("count","")))
                        except Exception:
                            pass
                    # sdo
                    for row in self.sdo_display:
                        try:
                            self.sdo_table.add_row(row.get("time",""), row.get("cob",""), row.get("dir", ""), row.get("name",""), row.get("index",""), row.get("sub",""), row.get("raw",""), str(row.get("decoded","")), str(row.get("count","")))
                        except Exception:
                            pass
                except Exception:
                    pass

                self.sdo_send_node.focus()
                self.sdo_send_node.action_select_all()

            async def on_button_pressed(self, event: Button.Pressed) -> None:
                """! Handle button press events from the Remote Node Control panel.
                @details
                Dispatches the button press to the appropriate handler based on
                which action button was pressed (Send SDO, Receive SDO, or Send PDO).
                - This method acts as a central event router for all Button widgets
                defined in the Remote Node Control UI.
                - The actual request construction and queueing logic is delegated
                to the corresponding helper methods.
                @param event Button press event containing the pressed button instance.
                @return None
                """

                btn = event.button

                if btn is self.sdo_send_btn:
                    self.notify("Send SDO request triggered", title="Send Action", severity="information")
                    self._send_sdo_request()
                elif btn is self.sdo_recv_btn:
                    self.notify("Receive SDO request triggered", title="Send Action", severity="information")
                    self._recv_sdo_request()
                elif btn is self.pdo_send_btn:
                    self.notify("Send PDO request triggered", title="Send Action", severity="information")
                    self._send_pdo()

            async def on_switch_changed(self, event: Switch.Changed) -> None:
                """! Handle repeat switch state changes.
                @details
                Enables or disables periodic transmission of SDO or PDO requests
                based on the state of the associated repeat switch.
                - When a repeat switch is enabled, a Textual timer is created using
                the configured interval value (in milliseconds).
                - When disabled, any existing timer for the corresponding action
                is stopped and removed.
                - Each repeat timer invokes the same request handler used for
                one-shot button presses.
                @param event Switch change event containing the toggled switch instance.
                @return None
                """

                sw = event.switch

                if sw is self.sdo_send_repeat:
                    state = "Enabled" if sw.value else "Disabled"
                    self.notify(
                        f"Send SDO repeat is now {state}",
                        title="Repeat Toggle",
                        severity="information"
                    )
                    self._toggle_repeat(
                        key="sdo_send",
                        enabled=sw.value,
                        interval_ms=self.sdo_send_repeat_value.value,
                        callback=self._send_sdo_request,
                    )

                elif sw is self.sdo_recv_repeat:
                    state = "Enabled" if sw.value else "Disabled"
                    self.notify(
                        f"Receive SDO repeat is now {state}",
                        title="Repeat Toggle",
                        severity="information"
                    )
                    self._toggle_repeat(
                        key="sdo_recv",
                        enabled=sw.value,
                        interval_ms=self.sdo_recv_repeat_value.value,
                        callback=self._recv_sdo_request,
                    )

                elif sw is self.pdo_repeat:
                    state = "Enabled" if sw.value else "Disabled"
                    self.notify(
                        f"Send PDO repeat is now {state}",
                        title="Repeat Toggle",
                        severity="information"
                    )
                    self._toggle_repeat(
                        key="pdo_send",
                        enabled=sw.value,
                        interval_ms=self.pdo_send_repeat_value.value,
                        callback=self._send_pdo,
                    )

            async def on_key(self, event: events.Key) -> None:
                """! Textual callback of detecting key press"""

                k = event.key
                try:
                    if k in ("q", "Q"):
                        await self.action_quit()
                        return
                except Exception:
                    # some textual versions may not allow awaiting action_quit here; fallback to stop
                    try:
                        self.exit()
                        return
                    except Exception:
                        pass

                # Copy/dump handlers mapped to single-letter keys
                if k in ("n", "N"):
                    dump = "== Protocol ==\n" + self._dump_table_rows(self.proto_table)
                    severity, msg = self._copy_to_clipboard_or_file(dump, "/tmp/canopen_protocol.txt")
                    self.notify(msg, title="Protocol Data", severity=severity)
                    return

                elif k in ("b", "B"):
                    # Bus Stats
                    dump = "== BUS STATS ==\n" + self._dump_table_rows(self.bus_stats_table)
                    severity, msg = self._copy_to_clipboard_or_file(dump, "/tmp/canopen_bus_stats.txt")
                    self.notify(msg, title="Bus Stats", severity=severity)
                    return

                elif k in ("p", "P"):
                    # PDO Data
                    dump = "== PDO ==\n" + self._dump_table_rows(self.pdo_table)
                    severity, msg = self._copy_to_clipboard_or_file(dump, "/tmp/canopen_pdo.txt")
                    self.notify(msg, title="PDO Data", severity=severity)

                elif k in ("s", "S"):
                    # SDO Data
                    dump = "== SDO ==\n" + self._dump_table_rows(self.sdo_table)
                    severity, msg = self._copy_to_clipboard_or_file(dump, "/tmp/canopen_sdo.txt")
                    self.notify(msg, title="SDO Data", severity=severity)

            def _copy_to_clipboard_or_file(self, text: str, filename: str = f"/tmp/{analyzer_defs.APP_NAME}.log"):
                """! Try to copy to clipboard using pyperclip; if unavailable, write to filename."""

                try:
                    pyperclip.copy(text)
                    return "information", "Copied to clipboard"
                except Exception:
                    self.logger.exception(f"Failed in copying table to clipboard, using temp file <{filename}> for fallback")
                    # fallback: write to tmp file
                    try:
                        with open(filename, "w", encoding="utf-8") as f:
                            f.write(text)
                        return "error", f"Wrote to {filename}"
                    except Exception as e:
                        self.logger.exception(f"Failed to copy/write: {e}")
                        return "error", f"Failed to copy/write: {e}"

            def _dump_table_rows(self, table) -> str:
                """! Return textual dump of a DataTable's rows.
                @details
                Tries several APIs depending on Textual version and avoids dumping internal RowKey objects.
                """

                lines = []
                try:
                    # Preferred: use row_count + get_row_at
                    if hasattr(table, "row_count") and getattr(table, "row_count"):
                        try:
                            for i in range(table.row_count):
                                try:
                                    row = table.get_row_at(i)
                                    if not row:
                                        continue
                                    if hasattr(row, "cells"):
                                        lines.append("	".join(str(c) for c in row.cells))
                                    elif isinstance(row, (list, tuple)):
                                        lines.append("	".join(str(c) for c in row))
                                    else:
                                        # fallback to str(row)
                                        lines.append(str(row))
                                except Exception:
                                    continue
                        except Exception:
                            pass
                    else:
                        # Fallback: try the table.rows mapping but attempt to convert values
                        rows_attr = getattr(table, "rows", None)
                        if rows_attr:
                            try:
                                for k, v in (rows_attr.items() if hasattr(rows_attr, 'items') else enumerate(rows_attr)):
                                    try:
                                        if hasattr(table, "get_row"):
                                            try:
                                                row = table.get_row(k)
                                                if row and hasattr(row, "cells"):
                                                    lines.append("	".join(str(c) for c in row.cells))
                                                    continue
                                            except Exception:
                                                pass

                                        if hasattr(v, "cells"):
                                            lines.append("	".join(str(c) for c in v.cells))
                                        elif isinstance(v, (list, tuple)):
                                            lines.append("	".join(str(c) for c in v))
                                        else:
                                            lines.append(str(v))
                                    except Exception:
                                        continue
                            except Exception:
                                pass
                except Exception:
                    pass
                return "\n".join(lines) if lines else "<no rows>"

            def _sparkline_text(self, history, width=None):
                """! Create a compact sparkline string from a numeric history sequence."""

                if not history:
                    return ""
                try:
                    seq = list(history)[- (width or analyzer_defs.STATS_GRAPH_WIDTH):]
                    if not seq:
                        return ""
                    blocks = "▁▂▃▄▅▆▇█"
                    mn, mx = min(seq), max(seq)
                    span = mx - mn or 1.0
                    chars = []
                    for v in seq:
                        try:
                            idx = int((float(v) - mn) / span * (len(blocks) - 1))
                        except Exception:
                            idx = 0
                        idx = max(0, min(idx, len(blocks) - 1))
                        chars.append(blocks[idx])
                    return "".join(chars)
                except Exception:
                    return ""

            def _update_from_queue(self) -> None:
                """! Poll processed_frame queue and update tables"""

                q = cls.processed_frame
                if q is None:
                    return
                got_any = False
                while True:
                    try:
                        pframe = q.get_nowait()
                    except Exception:
                        break
                    got_any = True
                    t = pframe.get("time", analyzer_defs.now_str())
                    cob = pframe.get("cob", 0)
                    ftype = pframe.get("type")
                    idx = pframe.get("index", 0)
                    sub = pframe.get("sub", 0)
                    name = pframe.get("name", "")
                    raw = pframe.get("raw", "")
                    decoded = pframe.get("decoded", "")
                    dirc = pframe.get("dir", "")

                    cob_s = f"0x{cob:03X}" if isinstance(cob, int) else str(cob)
                    idx_s = f"0x{idx:04X}" if isinstance(idx, int) else str(idx)
                    sub_s = f"0x{sub:02X}" if isinstance(sub, int) else str(sub)

                    type_name = ftype.name if isinstance(ftype, analyzer_defs.frame_type) else str(ftype)

                    # Consistently use cls.fixed (set by run_textual) to decide behavior.
                    if cls.fixed:
                        if ftype == analyzer_defs.frame_type.PDO:
                            key = (cob, idx, sub)
                            row = {"time": t, "cob": cob_s, "dir": dirc, "name": name, "index": idx_s, "sub": sub_s, "raw": raw, "decoded": str(decoded), "count": 1}
                            prev = self.fixed_pdo.get(key)
                            if prev:
                                row["count"] = prev.get("count", 1) + 1
                            self.fixed_pdo[key] = row
                        elif ftype in (analyzer_defs.frame_type.SDO_REQ, analyzer_defs.frame_type.SDO_RES):
                            key = (cob, idx, sub)
                            row = {"time": t, "cob": cob_s, "dir": dirc, "name": name, "index": idx_s, "sub": sub_s, "raw": raw, "decoded": str(decoded), "count": 1}
                            prev = self.fixed_sdo.get(key)
                            if prev:
                                row["count"] = prev.get("count", 1) + 1
                            self.fixed_sdo[key] = row
                        else:
                            key = (cob, type_name)
                            row = {"time": t, "cob": cob_s, "type": type_name, "raw": raw, "decoded": str(decoded), "count": 1}
                            prev = self.fixed_proto.get(key)
                            if prev:
                                row["count"] = prev.get("count", 1) + 1
                            self.fixed_proto[key] = row
                    else:
                        # scrolling mode
                        if ftype == analyzer_defs.frame_type.PDO:
                            row = {"time": t, "cob": cob_s, "dir": dirc, "name": name, "index": idx_s, "sub": sub_s, "raw": raw, "decoded": str(decoded), "count": 1}
                            placed = False
                            try:
                                for i in range(len(self.pdo_display)):
                                    if not self.pdo_display[i].get("time"):
                                        self.pdo_display[i] = row
                                        placed = True
                                        break
                            except Exception:
                                placed = False
                            if not placed:
                                try:
                                    self.pdo_display.pop(0)
                                    self.pdo_display.append(row)
                                except Exception:
                                    try:
                                        self.pdo_display[-1] = row
                                    except Exception:
                                        pass

                        elif ftype in (analyzer_defs.frame_type.SDO_REQ, analyzer_defs.frame_type.SDO_RES):
                            row = {"time": t, "cob": cob_s, "dir": dirc, "name": name, "index": idx_s, "sub": sub_s, "raw": raw, "decoded": str(decoded), "count": 1}
                            placed = False
                            try:
                                for i in range(len(self.sdo_display)):
                                    if not self.sdo_display[i].get("time"):
                                        self.sdo_display[i] = row
                                        placed = True
                                        break
                            except Exception:
                                placed = False
                            if not placed:
                                try:
                                    self.sdo_display.pop(0)
                                    self.sdo_display.append(row)
                                except Exception:
                                    try:
                                        self.sdo_display[-1] = row
                                    except Exception:
                                        pass

                        else:
                            row = {"time": t, "cob": cob_s, "type": type_name, "raw": raw, "decoded": str(decoded), "count": 1}
                            placed = False
                            try:
                                for i in range(len(self.proto_display)):
                                    if not self.proto_display[i].get("time"):
                                        self.proto_display[i] = row
                                        placed = True
                                        break
                            except Exception:
                                placed = False
                            if not placed:
                                try:
                                    self.proto_display.pop(0)
                                    self.proto_display.append(row)
                                except Exception:
                                    try:
                                        self.proto_display[-1] = row
                                    except Exception:
                                        pass


                    try:
                        q.task_done()
                    except Exception:
                        pass

                if got_any:
                    self._refresh_tables()

                # always refresh bus stats table (even if no new frames)
                self._refresh_bus_stats()

            def _clear_table_rows(self, table):
                """! Robustly clear all rows from a DataTable instance."""

                # Preferred: use clear(rows=True) if available
                try:
                    table.clear(rows=True)
                    return
                except Exception:
                    pass
                # Try clearing by row keys if remove_row exists
                try:
                    # DataTable may expose row_count and remove_row()
                    if hasattr(table, "row_count") and hasattr(table, "remove_row"):
                        # remove from end to start to avoid index shifting
                        count = table.row_count
                        for i in range(count - 1, -1, -1):
                            try:
                                table.remove_row(i)
                            except Exception:
                                # some implementations expect a key, try first key
                                try:
                                    keys = list(table.rows.keys())
                                    if keys:
                                        table.remove_row(keys[0])
                                except Exception:
                                    pass
                        return
                except Exception:
                    pass
                # Fallback: try to set rows to empty if attribute exists
                try:
                    if hasattr(table, "rows"):
                        table.rows = []
                        return
                except Exception:
                    pass
                # Last resort: recreate the widget in its parent container (best-effort)
                try:
                    parent = getattr(table, "parent", None)
                    if parent is not None and hasattr(parent, "remove"):
                        # attempt to preserve order
                        children = list(parent.children)
                        idx = children.index(table)
                        # create a new DataTable instance
                        new_table = DataTable(zebra_stripes=True)
                        parent.remove(table)
                        parent.mount(new_table, before=children[idx] if idx < len(children) else None)
                        # update references
                        if table is getattr(self, "proto_table", None):
                            self.proto_table = new_table
                        elif table is getattr(self, "pdo_table", None):
                            self.pdo_table = new_table
                        elif table is getattr(self, "sdo_table", None):
                            self.sdo_table = new_table
                        elif table is getattr(self, "bus_stats_table", None):
                            self.bus_stats_table = new_table
                        return
                except Exception:
                    pass

            def _refresh_tables(self):
                """! Refresh the three DataTables with either fixed-mode rows (replace) or scrolling rows (append last N).
                Ensures previous displayed rows are removed before adding new rows to avoid the table growing indefinitely.
                """

                # Clear existing rows robustly before adding new ones
                try:
                    self._clear_table_rows(self.proto_table)
                except Exception:
                    pass
                try:
                    self._clear_table_rows(self.pdo_table)
                except Exception:
                    pass
                try:
                    self._clear_table_rows(self.sdo_table)
                except Exception:
                    pass
                # reset fill indices when clearing so scrolling refills from top again
                try:
                    self.proto_next_index = 0
                    self.pdo_next_index = 0
                    self.sdo_next_index = 0
                except Exception:
                    pass

                # Protocol table rows
                if cls.fixed:
                    all_protos = list(self.fixed_proto.values())
                    real_protos = [r for r in all_protos if r.get("cob", "")]
                    blank_protos = [r for r in all_protos if not r.get("cob", "")]
                    real_protos_sorted = sorted(real_protos, key=lambda r: (r.get("cob", ""), r.get("type", "")))
                    protos = real_protos_sorted[:analyzer_defs.PROTOCOL_TABLE_HEIGHT]
                    remaining = analyzer_defs.PROTOCOL_TABLE_HEIGHT - len(protos)
                    if remaining > 0:
                        protos.extend(blank_protos[:remaining])
                        while len(protos) < analyzer_defs.PROTOCOL_TABLE_HEIGHT:
                            protos.append({"time": "", "cob": "", "type": "", "raw": "", "decoded": "", "count": ""})
                else:
                    protos = list(self.proto_display)

                # Add protocol rows (keeps whichever visual ordering you already use)
                for p in protos:
                    try:
                        self.proto_table.add_row(
                            p.get("time", ""),
                            p.get("cob", ""),
                            p.get("type", ""),
                            p.get("raw", ""),
                            p.get("decoded", ""),
                            str(p.get("count", "")),
                        )
                    except Exception:
                        pass


                # PDO table rows
                if cls.fixed:
                    all_pdos = list(self.fixed_pdo.values())
                    real_pdos = [r for r in all_pdos if r.get("cob", "")]
                    blank_pdos = [r for r in all_pdos if not r.get("cob", "")]
                    real_pdos_sorted = sorted(real_pdos, key=lambda r: (r.get("cob", ""), r.get("index", "")))
                    pdos = real_pdos_sorted[:analyzer_defs.DATA_TABLE_HEIGHT]
                    remaining = analyzer_defs.DATA_TABLE_HEIGHT - len(pdos)
                    if remaining > 0:
                        pdos.extend(blank_pdos[:remaining])
                        while len(pdos) < analyzer_defs.DATA_TABLE_HEIGHT:
                            pdos.append({"time": "", "cob": "", "dir": "", "name": "", "index": "", "sub": "", "raw": "", "decoded": "", "count": ""})
                else:
                    # scrolling mode uses the display buffer / deque (keep as-is)
                    pdos = list(self.pdo_display)

                for p in pdos:
                    try:
                        self.pdo_table.add_row(
                            p.get("time", ""),
                            p.get("cob", ""),
                            p.get("dir", ""),
                            p.get("name", ""),
                            p.get("index", ""),
                            p.get("sub", ""),
                            p.get("raw", ""),
                            str(p.get("decoded", "")),
                            str(p.get("count", "")),
                        )
                    except Exception:
                        pass


                # SDO table rows
                if cls.fixed:
                    all_sdos = list(self.fixed_sdo.values())
                    real_sdos = [r for r in all_sdos if r.get("cob", "")]
                    blank_sdos = [r for r in all_sdos if not r.get("cob", "")]
                    real_sdos_sorted = sorted(real_sdos, key=lambda r: (r.get("cob", ""), r.get("index", "")))
                    sdos = real_sdos_sorted[:analyzer_defs.DATA_TABLE_HEIGHT]
                    remaining = analyzer_defs.DATA_TABLE_HEIGHT - len(sdos)
                    if remaining > 0:
                        sdos.extend(blank_sdos[:remaining])
                        while len(sdos) < analyzer_defs.DATA_TABLE_HEIGHT:
                            sdos.append({"time": "", "cob": "", "dir": "", "name": "", "index": "", "sub": "", "raw": "", "decoded": "", "count": ""})
                else:
                    sdos = list(self.sdo_display)

                for s in sdos:
                    try:
                        self.sdo_table.add_row(
                            s.get("time", ""),
                            s.get("cob", ""),
                            s.get("dir", ""),
                            s.get("name", ""),
                            s.get("index", ""),
                            s.get("sub", ""),
                            s.get("raw", ""),
                            str(s.get("decoded", "")),
                            str(s.get("count", "")),
                        )
                    except Exception:
                        pass

            def _refresh_bus_stats(self):
                """! Populate the bus_stats_table DataTable using the stats snapshot."""

                snapshot = cls.stats.get_snapshot() if cls.stats else None
                # If no snapshot, clear and show placeholder
                if not snapshot:
                    try:
                        self._clear_table_rows(self.bus_stats_table)
                        self.bus_stats_table.add_row("State", "No stats", "")
                    except Exception:
                        pass
                    return

                # Clear previous rows
                try:
                    self._clear_table_rows(self.bus_stats_table)
                except Exception:
                    pass

                # Basic values
                total_frames = getattr(snapshot.frame_count, "total", 0)
                nodes = getattr(snapshot, "nodes", {}) or {}

                # Read rates and histories from snapshot.rates (structure provided by bus_stats)
                rates_latest = getattr(snapshot.rates, "latest", {}) if hasattr(snapshot, "rates") else {}
                rates_hist = getattr(snapshot.rates, "history", {}) if hasattr(snapshot, "rates") else {}

                def get_hist(key):
                    """! Helper to get stats history"""

                    try:
                        return rates_hist.get(key, []) if isinstance(rates_hist, dict) else []
                    except Exception:
                        return []

                def add_metric(label, value, hist_key=None, data=None):
                    """! Add a single Bus Statistics metric row to the TUI table.
                    @details
                    This helper inserts one row into the Bus Stats DataTable.
                    The row consists of:
                    - A metric label (left column)
                    - A value (middle column)
                    - Either a sparkline graph or custom renderable (right column)
                    @note
                    The graph column behavior depends on the inputs:
                    - If `hist_key` is provided, a sparkline is generated using
                      historical data associated with that key.
                    - Otherwise, `data` is used directly as the graph/renderable.

                    A fallback path is included to handle rendering issues by
                    coercing all fields to strings if necessary.
                    @param label Metric table to be displayed.
                    @param value Value corresponding to the label.
                    @param hist_key Historical data.
                    @param data Data to be used as the graph.
                    """

                    ## Content for the Graph column to render.
                    graph = ""

                    ## Generate sparkline graph when a history key is provided.
                    if hist_key:
                        ## Retrieve history samples for the given key.
                        hist = get_hist(hist_key)

                        ## Convert history samples into a sparkline render.
                        graph = self._sparkline_text(hist)
                    else:
                        ## Use explicitly provided render/data for graph column.
                        graph = data

                    try:
                        ## Primary path: add row using native renders.
                        self.bus_stats_table.add_row(label, value, graph)
                    except Exception:
                        try:
                            ## Fallback path: coerce all fields to strings
                            ## to avoid DataTable rendering failures.
                            self.bus_stats_table.add_row(
                                label,
                                str(value),
                                str(graph)
                            )
                        except Exception:
                            ## Suppress any remaining rendering errors to
                            ## keep the TUI responsive.
                            pass

                # Bus state (authoritative, from bus_stats)
                bus_state = getattr(snapshot.rates, "bus_state", "Idle")
                add_metric("State", bus_state)

                # Active Nodes (count + node IDs)
                nodes = sorted(snapshot.nodes) if snapshot.nodes else []

                node_count = str(len(nodes))

                if nodes:
                    node_ids = Text(
                        "[" + ", ".join(f"0x{n:02X}" for n in nodes) + "]",
                        style="bold white"
                    )
                else:
                    node_ids = Text("")

                add_metric("Active Nodes", node_count, hist_key=None, data=node_ids)

                # PDO
                pdo_val = float(rates_latest.get("pdo", 0.0)) if isinstance(rates_latest, dict) else 0.0
                add_metric("PDO Frames/s", f"{pdo_val:.1f}", "pdo")

                # SDO (request + response)
                sdo_res = float(rates_latest.get("sdo_res", 0.0)) if isinstance(rates_latest, dict) else 0.0
                sdo_req = float(rates_latest.get("sdo_req", 0.0)) if isinstance(rates_latest, dict) else 0.0
                sdo_val = sdo_res + sdo_req
                # build combined history (element wise sum when lengths match)
                sdo_hist_res = get_hist("sdo_res")
                sdo_hist_req = get_hist("sdo_req")
                sdo_hist = []
                try:
                    if sdo_hist_res and sdo_hist_req and len(sdo_hist_res) == len(sdo_hist_req):
                        sdo_hist = [a + b for a, b in zip(sdo_hist_res, sdo_hist_req)]
                    elif sdo_hist_res:
                        sdo_hist = list(sdo_hist_res)
                    elif sdo_hist_req:
                        sdo_hist = list(sdo_hist_req)
                except Exception:
                    sdo_hist = list(sdo_hist_res) if sdo_hist_res else list(sdo_hist_req) if sdo_hist_req else []
                # add combined SDO graph
                try:
                    self.bus_stats_table.add_row("SDO Frames/s", f"{sdo_val:.1f}", self._sparkline_text(sdo_hist))
                except Exception:
                    pass

                # Heart beat
                hb_val = float(rates_latest.get("hb", 0.0)) if isinstance(rates_latest, dict) else 0.0
                add_metric("HB Frames/s", f"{hb_val:.1f}", "hb")

                # Emergency Messages
                emcy_val = float(rates_latest.get("emcy", 0.0)) if isinstance(rates_latest, dict) else 0.0
                add_metric("EMCY Frames/s", f"{emcy_val:.1f}", "emcy")

                # Total frames/s
                total_val = float(rates_latest.get("total", 0.0)) if isinstance(rates_latest, dict) else 0.0
                add_metric("Total Frames/s", f"{total_val:.1f}", "total")

                # Peak frames/s
                peak_val = float(getattr(snapshot.rates, "peak_fps", 0.0))
                add_metric("Peak Frames/s", f"{peak_val:.1f}")

                # Bus utilization (computed by bus_stats)
                util = None
                if hasattr(snapshot, "rates") and hasattr(snapshot.rates, "bus_util_percent"):
                    util = snapshot.rates.bus_util_percent
                elif hasattr(snapshot, "compute_bus_util"):
                    try:
                        util = snapshot.compute_bus_util()
                    except Exception:
                        util = None

                idle = max(0.0, 100.0 - util) if util is not None else 0.0
                add_metric("Bus Util %", f"{util:.2f}%" if util is not None else "-", "total")
                add_metric("Bus Idle %", f"{idle:.2f}%" if util is not None else "-")

                # SDO stats & response time
                try:
                    add_metric("SDO OK/Abort", f"{snapshot.sdo.success}/{snapshot.sdo.abort}")
                    avg_sdo_rt = (sum(snapshot.sdo.response_time) / len(snapshot.sdo.response_time)) if snapshot.sdo.response_time else 0.0
                    add_metric("SDO resp time", f"{avg_sdo_rt * 1000:.1f} ms")
                except Exception:
                    add_metric("SDO OK/Abort", "-")
                    add_metric("SDO resp time", "-")

                # Last error frame
                last_err = "-"
                try:
                    if snapshot.error.last_time or snapshot.error.last_frame:
                        last_err = f"[{snapshot.error.last_time}] <{snapshot.error.last_frame}>"
                except Exception:
                    last_err = "-"
                add_metric("Last Error Frame", last_err)

                # Top talkers
                try:
                    top = snapshot.top_talkers.most_common(analyzer_defs.MAX_STATS_SHOW)
                    top_str = ", ".join(f"0x{c:03X}:{cnt}" for c, cnt in top) if top else "-"
                    add_metric("Top Talkers", top_str)
                except Exception:
                    add_metric("Top Talkers", "-")

                # Frame distribution — show top-N kinds sorted by count (descending)
                try:
                    counts = snapshot.frame_count.counts
                    items = sorted(((k.name, v) for k, v in counts.items()), key=lambda kv: kv[1], reverse=True)
                    shown = items[:analyzer_defs.MAX_STATS_SHOW]
                    dist_pairs = ", ".join(f"{name}:{cnt}" for name, cnt in shown) if shown else "-"
                except Exception:
                    dist_pairs = "-"
                add_metric("Frame Dist.", dist_pairs)

                # keep a textual cache for copy operations
                try:
                    lines = []
                    for i in range(self.bus_stats_table.row_count if hasattr(self.bus_stats_table, 'row_count') else 0):
                        try:
                            row = self.bus_stats_table.get_row_at(i)
                            if row and hasattr(row, 'cells'):
                                lines.append("\t".join(str(c) for c in row.cells))
                        except Exception:
                            pass
                    self._last_bus_stats = "\n".join(lines)
                except Exception:
                    self._last_bus_stats = None

            def _send_sdo_request(self):
                """! Send an SDO download (write) request.
                @details
                Builds an SDO download request from the Send SDO UI fields
                (node ID, index, sub-index, value, and size) and enqueues it
                into the shared requested_frame queue for processing by the
                CANopen backend.
                - All numeric fields are parsed using base autodetection (base=0).
                - The SDO size is obtained from the currently selected Size radio button.
                - On parsing or validation failure, an error notification is shown
                    to the user and no request is queued.
                @return None
                """

                try:
                    req = {
                        "type": "sdo_download",
                        "node": int(self.sdo_send_node.value, 0),
                        "index": int(self.sdo_send_index.value, 0),
                        "sub": int(self.sdo_send_sub.value, 0),
                        "value": int(self.sdo_send_value.value, 0),
                        "size": self._get_selected_sdo_size()
                    }
                    cls.requested_frame.put(req)
                except Exception as e:
                    self.notify(str(e), title="Send Action", severity="error")

            def _recv_sdo_request(self):
                """! Send an SDO upload (read) request.
                @details
                Builds an SDO upload request from the Receive SDO UI fields
                (node ID, index, and sub-index) and enqueues it into the
                shared requested_frame queue.
                - All numeric fields are parsed using base autodetection (base=0).
                - Any parsing or validation error is reported to the user
                via a notification and the request is not queued.
                @return None
                """

                try:
                    req = {
                        "type": "sdo_upload",
                        "node": int(self.sdo_recv_node.value, 0),
                        "index": int(self.sdo_recv_index.value, 0),
                        "sub": int(self.sdo_recv_sub.value, 0),
                    }
                    cls.requested_frame.put(req)
                except Exception as e:
                    self.notify(str(e), title="Send Action", severity="error")

            def _send_pdo(self):
                """! Send a PDO frame.
                @details
                Parses the PDO COB-ID and data payload from the Send PDO UI fields,
                converts the hexadecimal data string into a byte array, and enqueues
                the PDO request into the shared requested_frame queue.
                - The data field is expected to be a space-separated hexadecimal string.
                - Invalid hex values or parsing errors will trigger a user notification
                and prevent the request from being queued.
                @return None
                """

                try:
                    data = bytes(int(b, 16) for b in str(self.pdo_data.value).split())
                    req = {
                        "type": "pdo",
                        "cob": int(self.pdo_cob.value, 0),
                        "data": data,
                    }
                    cls.requested_frame.put(req)
                except Exception as e:
                    self.notify(str(e), title="Send Action", severity="error")

            def _get_selected_sdo_size(self) -> int:
                """! Get the selected SDO data size.
                @details
                Determines the currently selected SDO size from the Size RadioButton
                group in the Send SDO UI.
                - Iterates over all RadioButton widgets in the size selector.
                - The selected button is identified by its boolean value state.
                - The label text of the selected button is converted to an integer.
                - If no selection is found, a default size of 1 is returned.
                @return int Selected SDO size (default = 1).
                """

                for btn in self.sdo_send_size.query(RadioButton):
                    if btn.value:
                        return int(str(btn.label))

                return 1

            def _toggle_repeat(self, key: str, enabled: bool, interval_ms: str, callback):
                """! Enable or disable a repeating request timer.
                    @details
                    Starts or stops a periodic timer that repeatedly invokes the given
                    callback function at the specified interval.
                    - If a timer already exists for the given key, it is stopped and removed.
                    - When disabled, no new timer is created.
                    - The interval is specified in milliseconds and internally converted
                      to seconds.
                    - A minimum interval of 50 ms is enforced to avoid excessive scheduling.
                    @param key Unique identifier for the repeat timer.
                    @param enabled Whether the repeat timer should be active.
                    @param interval_ms Repeat interval in milliseconds (string input).
                    @param callback Callable to invoke on each timer tick.
                    @return None
                """

                # stop existing timer
                if key in self._repeat_tasks:
                    try:
                        self._repeat_tasks[key].stop()
                    except Exception:
                        pass
                    del self._repeat_tasks[key]

                if not enabled:
                    return

                try:
                    interval = max(0.05, int(interval_ms) / 1000.0)
                except Exception:
                    interval = 1.0

                self._repeat_tasks[key] = self.set_interval(interval, callback)


            # Textual CSS styles for titles
            CSS = r'''

            .left.column {
                padding-right: 1;
            }
            .right.column {
                padding-left: 1;
            }

            .header.protocol {
                color: seagreen;
                content-align: center middle;
                background: lightgrey;
            }
            .table.protocol {
                color: seagreen;
                content-align: center middle;
            }
            .header.busstats {
                color: peru;
                background: lightgrey;
                text-style: bold;
                content-align: center middle;
            }
            .table.busstats {
                color: peru;
                content-align: center middle;
            }

            .header.pdo {
                color: slateblue;
                background: lightgrey;
                text-style: bold;
                content-align: center middle;
            }
            .table.pdo {
                color: slateblue;
                content-align: center middle;
            }

            .header.sdo {
                color: mediumorchid;
                background: lightgrey;
                text-style: bold;
                content-align: center middle;
            }
            .table.sdo {
                color: mediumorchid;
                content-align: center middle;
            }

            .root.remote {
                max-height: 15;
            }
            .header.remote {
                color: royalblue;
                background: lightgrey;
                text-style: bold;
                content-align: center middle;
            }
            .subheader.remote.sdo.send {
                color: white;
                background: steelblue;
                content-align: center middle;
            }
            .subheader.remote.sdo.receive {
                color: black;
                background: darkseagreen;
                content-align: center middle;
            }
            .subheader.remote.pdo.send {
                color: black;
                background: goldenrod;
                content-align: center middle;
            }
            .row.remote {
                content-align: center middle;
            }
            .column.remote {
                padding: 0 1;
                content-align: left middle;
            }

            .content.remote.sdo.send {
                color: steelblue;
                text-style: bold;
                content-align: left top;
            }
            .content.remote.sdo.receive {
                color: darkseagreen;
                text-style: bold;
                content-align: left top;
            }
            .content.remote.pdo.send {
                color: goldenrod;
                text-style: bold;
                content-align: left top;
            }
            .button.remote {
                color: white;
                background: grey;
                text-style: bold;
            }
            .radio.remote {
                layout: horizontal;
                padding: 0 1;
                text-style: bold;
            }
            '''

        # run the textual app (blocking)
        tui_app().run()