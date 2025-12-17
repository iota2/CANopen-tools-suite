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
@file display_gui.py
@brief Single-screen PySide6 (Qt6) GUI backend for CANopen Analyzer.

@details
This module provides the Qt GUI implementation corresponding to
CLI. All terminology, operating modes (Fixed / Sequential),
and data semantics intentionally mirror the CLI to ensure consistency
between user interfaces.

The GUI layer is responsible only for presentation and interaction:
- Rendering decoded CANopen traffic in tables
- Displaying live bus statistics and frame-rate histories
- Managing user input, layout persistence, and shutdown behavior

The GUI is intentionally conservative in logic: all CANopen semantics,
rate calculations, and statistics are owned by bus_stats and related
backend modules.
"""

import sys
import queue
import signal

from PySide6.QtCore import (
    Qt, QObject, Signal, QThread, QEvent,
    QSettings, QTimer, QMargins
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTableWidget, QTableWidgetItem, QDockWidget, QSplitter,
    QPushButton, QLineEdit, QComboBox, QToolBar, QToolTip,
    QLabel, QHeaderView
)
from PySide6.QtCharts import (
    QChart, QChartView, QLineSeries, QValueAxis
)
from PySide6.QtGui import (
    QAction, QPainter, QColor,
    QCursor, QFont, QPen
)

import analyzer_defs as analyzer_defs
from bus_stats import bus_stats


class GUIUpdateWorker(QObject):
    """! Background worker for delivering decoded CAN frames to the GUI.
    @details
    Mirrors the producer/consumer pattern used by CLI,
    The worker runs in a dedicated QThread and emits decoded frames
    via a Qt signal to avoid cross-thread UI access.
    """

    ## Emitted when a decoded frame is available
    frame_received = Signal(dict)

    def __init__(self, processed_frame: queue.Queue):
        """! Initialize the worker.
        @param processed_frame Thread-safe queue of decoded CAN frames.
        """

        super().__init__()

        ## Processed frame received from CAN thread
        self.processed_frame = processed_frame
        self._running = True

    def run(self):
        """! Run the worker."""

        while self._running:
            try:
                frame = self.processed_frame.get(timeout=0.1)
                self.frame_received.emit(frame)
                self.processed_frame.task_done()
            except queue.Empty:
                pass

    def stop(self):
        """! Stop the worker."""

        self._running = False


class MultiRateLineWidget(QWidget):
    """
    Multi-series FPS graph widget (full-width graph).

    UX:
      - Graph spans full width
      - Series name + FPS overlaid on top of graph

    Functionality:
      - Identical behavior to grid-based version
      - Same update(), clear(), tooltip semantics
    """

    def __init__(self, series_defs):
        """! Frame rate widget initialization."""
        super().__init__()

        ## Maximum number of samples retained per rate series
        self.max_points = analyzer_defs.STATS_GRAPH_WIDTH

        ## Mapping: series name -> QLineSeries (main FPS line)
        self.series = {}

        ## Mapping: series name -> human-readable color name (for tooltips)
        self.series_colors = {}

        ## Mapping: series name -> QLabel displaying current FPS
        self.value_labels = {}

        ## Mapping: series name -> rolling FPS history list
        self._history = {}

        # ------------------------------------------------------------------
        # Root layout for the widget
        # ------------------------------------------------------------------
        root = QVBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2)
        root.setSpacing(4)

        # ------------------------------------------------------------------
        # Header area (textual FPS values above the chart)
        # ------------------------------------------------------------------
        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(2, 2, 2, 2)
        header_layout.setSpacing(2)

        ## Layout used later to align header text with chart plot area
        self.header_layout = header_layout

        ## Mapping: series name -> header QLabel
        self.header_labels = {}

        # Use monospace font to keep FPS values visually aligned
        font = QFont()
        font.setFamily("Monospace")
        font.setStyleHint(QFont.Monospace)
        font.setBold(True)

        # Create one header label per rate series
        for name, color in series_defs:
            lbl = QLabel(f"{name}: 0.0 fps")
            lbl.setFont(font)
            lbl.setStyleSheet(f"color: {QColor(color).name()};")
            header_layout.addWidget(lbl)
            self.header_labels[name] = lbl

        # Stretch pushes header labels to the left
        header_layout.addStretch(1)

        root.addWidget(header)

        # ------------------------------------------------------------------
        # Chart setup (shared time axis for all rate series)
        # ------------------------------------------------------------------
        ## Chart object to display frame rate graphs.
        self.chart = QChart()
        self.chart.setBackgroundVisible(False)
        self.chart.legend().hide()
        self.chart.setMargins(QMargins(2, 2, 2, 2))

        ## X-axis represents sample index / time progression.
        self.axis_x = QValueAxis()
        self.axis_x.setLabelFormat("%d")
        self.axis_x.setTickCount(6)
        self.axis_x.setGridLineVisible(True)
        self.axis_x.setGridLineColor(QColor(220, 220, 220))

        ## Y-axis represents frames per second.
        self.axis_y = QValueAxis()
        self.axis_y.setLabelFormat("%.0f")
        self.axis_y.setTickCount(5)
        self.axis_y.setGridLineVisible(True)
        self.axis_y.setGridLineColor(QColor(220, 220, 220))

        self.chart.addAxis(self.axis_x, Qt.AlignBottom)
        self.chart.addAxis(self.axis_y, Qt.AlignLeft)

        ## Mapping: series name -> QLineSeries (main line).
        self.series = {}

        ## Mapping: series name -> QLineSeries (peak indicator line).
        self.peak_lines = {}

        ## Mapping: series name -> color name string.
        self.series_colors = {}

        # ------------------------------------------------------------------
        # Create chart series for each rate metric
        # ------------------------------------------------------------------
        for name, color in series_defs:
            # ---- Main FPS line ----
            s = QLineSeries()
            s.setColor(color)
            self.chart.addSeries(s)
            s.attachAxis(self.axis_x)
            s.attachAxis(self.axis_y)
            self.series[name] = s

            # ---- Peak FPS line (horizontal, dotted, faded) ----
            # Indicates maximum observed FPS in current history window
            peak = QLineSeries()
            peak_color = QColor(color)
            peak_color.setAlpha(80)
            peak.setColor(peak_color)
            peak.setPen(QPen(peak_color, 1, Qt.DotLine))

            self.chart.addSeries(peak)
            peak.attachAxis(self.axis_x)
            peak.attachAxis(self.axis_y)
            self.peak_lines[name] = peak

            # Store human-readable color name for tooltips
            self.series_colors[name] = self._color_name(QColor(color))

        # ------------------------------------------------------------------
        # Chart view widget
        # ------------------------------------------------------------------
        ## Chart view widget for frame rate graphs.
        self.view = QChartView(self.chart)
        self.view.setRenderHint(QPainter.Antialiasing)

        # Height bounds keep graph readable but compact in the right dock
        self.view.setMinimumHeight(100)
        self.view.setMaximumHeight(200)

        # Enable mouse tracking for hover tooltips
        self.view.setMouseTracking(True)
        self.view.viewport().setMouseTracking(True)
        self.view.viewport().installEventFilter(self)

        # Ensure the widget reserves enough vertical space
        self.setMinimumHeight(200)

        # Visual framing for the rate widget
        self.setStyleSheet("""
            MultiRateLineWidget {
                border: 1px solid palette(mid);
                border-radius: 4px;
                margin: 0px;
                padding: 2px;
                background-color: transparent;
            }
        """)

        root.addWidget(self.view)

    def _align_header_with_plot(self):
        """! Align the starting of header text with the chart plot area."""

        # Retrieve the current plot area rectangle of the chart.
        # This represents the drawable region excluding axes, labels, and margins.
        plot = self.chart.plotArea()

        # Extract the left X-coordinate of the plot area.
        # This offset corresponds to the space occupied by the Y-axis and its labels.
        left = int(plot.left())

        # Apply a matching left margin to the header layout so that
        # header labels (e.g. \"PDO\", \"SDO-Req\") align vertically with
        # the start of the graph's data region, not the chart frame.
        #
        # Top/Bottom margins are kept minimal to preserve compact vertical layout.
        self.header_layout.setContentsMargins(left, 2, 6, 2)

    def _color_name(self, color: QColor) -> str:
        """! Convert a QColor to a human-readable color name.
        @details
        Used primarily for tooltips and diagnostics to present
        meaningful color names (e.g. \"Dark Blue\") instead of
        raw hex color codes. If a color is not part of the
        predefined mapping, the QColor hex name is returned.
        @param color QColor instance representing the series color.
        @return Human-readable color name string if known, otherwise
                the QColor hex name (e.g. \"#3a7bd5\").
        """

        # Mapping of known Qt global colors (by hex value) to
        # human-readable color names for display purposes.
        mapping = {
            QColor(Qt.darkBlue).name(): "Dark Blue",
            QColor(Qt.darkCyan).name(): "Dark Cyan",
            QColor(Qt.darkGreen).name(): "Dark Green",
            QColor(Qt.darkYellow).name(): "Dark Yellow",
            QColor(Qt.darkRed).name(): "Dark Red",
        }

        # Return a friendly name if known; otherwise fall back
        # to the QColor hex string (e.g. \"#3a7bd5\").
        return mapping.get(color.name(), color.name())

    def clear(self):
        """! Clear all rate graph data and reset display state.
        @details
        Resets the widget to its initial state by:
        - Clearing all plotted FPS series
        - Discarding stored history buffers
        - Resetting displayed FPS values to zero
        - Restoring chart axes to safe default ranges
        @note
        This method is typically invoked when the user presses
        the *Clear* action or when switching display modes
        (Fixed / Sequential).
        """

        # Clear all main and peak line series from the chart
        for series in self.series.values():
            series.clear()

        # Reset rolling FPS history buffers for each series
        for name in self._history:
            self._history[name] = []

        # Reset textual FPS value labels in the header
        for lbl in self.value_labels.values():
            lbl.setText("  0.0 fps")

        # Reset X-axis to the full history window
        # (minimum of 1 prevents a zero-width axis)
        self.axis_x.setRange(0, max(1, self.max_points))

        # Reset Y-axis to a minimal range to keep the chart visible
        self.axis_y.setRange(0, 1.0)

    def update(self, values: dict, histories: dict):
        """! Update rate graph with new FPS values and histories.
        @details
        Updates both the textual FPS indicators and the plotted
        history for each configured rate series. This method:
        - Truncates history to the configured window size
        - Redraws all series with the latest history
        - Updates peak indicators per series
        - Rescales the Y-axis based on the maximum observed FPS
        @note
        The widget performs no rate calculations itself; it
        only visualizes values supplied by the caller, mirroring
        the CLI rate display behavior.
        @param values Mapping of series name to current FPS value.
        @param histories Mapping of series name to rolling FPS history list.
        """

        ## Track maximum FPS across all series for Y-axis scaling
        ymax = 0.0

        # Iterate over all configured rate series
        for name, series in self.series.items():
            # Retrieve and clamp history to the maximum window size
            hist = list(histories.get(name, []))[-self.max_points:]
            self._history[name] = hist

            # Redraw the main FPS line using the current history
            series.clear()
            for i, v in enumerate(hist):
                series.append(i, float(v))

            # Track peak FPS across all series for axis scaling
            if hist:
                ymax = max(ymax, max(hist))

            # Update textual FPS indicator in the header
            self.header_labels[name].setText(
                f"{name}: {values.get(name, 0.0):4.1f} fps"
            )

            # Update peak indicator line for this series
            peak_series = self.peak_lines[name]
            peak_series.clear()

            # Draw a horizontal dotted line at the maximum FPS
            # observed within the current history window
            if hist:
                peak_val = max(hist)
                peak_series.append(0, peak_val)
                peak_series.append(self.max_points, peak_val)

        # Reset X-axis to span the full history window
        # (minimum of 1 avoids a zero-width axis)
        self.axis_x.setRange(0, max(1, self.max_points))

        # Scale Y-axis slightly above the maximum observed FPS
        # to provide visual headroom above plotted lines
        self.axis_y.setRange(0, max(1.0, ymax * 1.2))


    def resizeEvent(self, event):
        """! Handle widget resize events.
        @details
        Recomputes header alignment after layout or size changes
        to ensure that header text remains vertically aligned
        with the chart plot area
        @param event Qt resize event containing the new widget geometry.
        """

        # Allow the base QWidget implementation to handle resizing
        super().resizeEvent(event)

        # Realign header text with the left edge of the chart plot area
        self._align_header_with_plot()

    def eventFilter(self, obj, event):
        """! Handle mouse hover events for the rate graph.
        @details
        Intercepts mouse-move events on the chart viewport to display
        a tooltip containing statistical information for each rate
        series. The tooltip shows:
        - Minimum FPS
        - Average FPS
        - Maximum FPS
        over the currently retained history window.
        @note
        Tooltip handling is intentionally limited to the chart's
        viewport so that labels and surrounding widgets do not
        trigger spurious updates.
        @param obj QObject that generated the event.
        @param event QEvent instance describing the event.
        @return True if the event is handled by this filter,
                otherwise delegates to the base implementation.
        """

        # Only handle mouse-move events originating from the chart viewport
        if obj is self.view.viewport() and event.type() == QEvent.MouseMove:
            lines = []

            # Iterate over all stored history buffers to compute statistics
            for name, hist in self._history.items():
                # Skip series with no available history
                if not hist:
                    continue

                # Compute basic statistics for the tooltip
                mn = min(hist)
                mx = max(hist)
                avg = sum(hist) / len(hist)

                # Retrieve human-readable color name for display
                color_name = self.series_colors.get(name, "Unknown")

                # Format tooltip block for this series
                lines.append(
                    f"{name} ({color_name})\n"
                    f"  Min: {mn:.2f} fps\n"
                    f"  Avg: {avg:.2f} fps\n"
                    f"  Max: {mx:.2f} fps"
                )

            # Display tooltip near the current cursor position
            if lines:
                QToolTip.showText(QCursor.pos(), "\n\n".join(lines))

            # Indicate that the event has been handled
            return True

        # Delegate unhandled events to the base class implementation
        return super().eventFilter(obj, event)


class CANopenMainWindow(QMainWindow):
    """! Main application window for the CANopen Analyzer GUI.
    @details
    Owns all top-level GUI components including menus, toolbars,
    dock widgets, central data tables, and rate graphs. This class
    is responsible for translating decoded CAN frames and bus
    statistics into visual updates.
    @note
    Terminology, display modes (Fixed / Sequential), and update
    behavior intentionally mirror CLI to maintain
    consistency between the CLI and GUI interfaces.
    """

    ## Background color used to temporarily highlight rows when
    ## new or updated data is received (activity heat effect).
    HEAT_COLOR = QColor(255, 230, 170)

    ## Duration (in milliseconds) for which the activity highlight
    ## remains visible before being cleared automatically.
    HEAT_CLEAR_MS = 600

    def __init__(self, stats: bus_stats, fixed: bool):
        """! Construct the main CANopen Analyzer GUI window.
        @details
        Initializes the main window state, stores shared backend
        references, constructs all UI components, and restores
        any previously saved layout. The initial display mode
        (Fixed / Sequential) is derived from the caller and
        mirrors the behavior of the CLI implementation.
        @param stats Shared bus statistics object used as the
                    single source of truth for counters and rates.
        @param fixed If True, start in Fixed (aggregated-row) mode;
                    otherwise start in Sequential mode.
        """

        # Initialize QMainWindow base class
        super().__init__()

        ## Reference to shared bus statistics backend
        self.stats = stats

        ## Display mode flag:
        ##  - True  -> Fixed (aggregated) display
        ##  - False -> Sequential (rolling) display
        self.fixed = fixed

        # Keys correspond to table row identifiers; values store
        # the most recent row data for efficient in-place updates.
        ## Fixed-row protocol data caches used when operating in Fixed mode.
        self.fixed_proto = {}
        ## Fixed-row PDO data caches used when operating in Fixed mode.
        self.fixed_pdo = {}
        ## Fixed-row SDO data caches used when operating in Fixed mode.
        self.fixed_sdo = {}

        ## Persistent settings store used for window geometry,
        ## dock layout, and column width restoration.
        self.settings = QSettings(analyzer_defs.APP_ORG, analyzer_defs.APP_NAME)

        # Set application window title and launch in maximized state
        self.setWindowTitle(analyzer_defs.APP_NAME)
        self.showMaximized()

        # ------------------------------------------------------------------
        # UI construction sequence
        # ------------------------------------------------------------------
        # Build menu bar, toolbar, and dock widgets before central content
        # to ensure consistent docking behavior and layout restoration.
        self._build_menu()
        self._build_toolbar()
        self._build_left_dock()
        self._build_right_dock()
        self._build_central()

        # Restore window geometry, dock positions, and splitter state
        self._restore_layout()

    def _build_menu(self):
        """! Build the main application menu bar.
        @details
        Initializes top-level menus for the GUI. The menu structure
        mirrors the CLI command groupings and is intentionally minimal,
        with additional actions expected to be added incrementally.
        """

        # Retrieve the QMainWindow menu bar instance
        menubar = self.menuBar()

        # Placeholder menu for export-related actions
        menubar.addMenu("Export")

        # Placeholder menu for view/layout-related actions
        menubar.addMenu("View")


    def _build_toolbar(self):
        """! Build the top control toolbar.
        @details
        Creates the primary control toolbar that provides quick-access
        actions affecting data flow and presentation, including:
        - Pause (future extension)
        - Clear (reset all tables, graphs, and statistics)
        - Mode selection (Fixed / Sequential)
        @note
        This toolbar corresponds conceptually to interactive controls
        available in the CLI.
        """

        # Create the toolbar and assign a stable object name
        # (required for layout persistence via QSettings)
        tb = QToolBar("Control Options")
        tb.setObjectName("ControlOptionsToolbar")

        # Dock the toolbar at the top of the main window
        self.addToolBar(Qt.TopToolBarArea, tb)

        # Pause action (currently a placeholder for future flow control)
        tb.addAction(QAction("Pause", self))

        # Clear action: resets tables, graphs, and backend statistics
        clear_act = QAction("Clear", self)
        clear_act.triggered.connect(self.clear_tables)
        tb.addAction(clear_act)

        # Visual separator between action buttons and mode selector
        tb.addSeparator()

        # Mode selector label
        tb.addWidget(QLabel("Mode:"))

        ## Mode selection combo box
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Fixed", "Sequential"])

        # Initialize combo box state from constructor argument
        self.mode_combo.setCurrentText(
            "Fixed" if self.fixed else "Sequential"
        )

        # React to mode changes by rebuilding displayed data
        self.mode_combo.currentTextChanged.connect(self._on_mode_changed)
        tb.addWidget(self.mode_combo)

    def _on_mode_changed(self, text):
        """! Handle display mode changes from the toolbar.
        @details
        Switches between Fixed (aggregated-row) and Sequential
        display modes. When the mode changes, all tables and
        graphs are cleared to avoid mixing incompatible data
        representations.
        @param text Selected mode string from the combo box.
        """

        # Update internal mode flag based on selected text
        self.fixed = (text == "Fixed")

        # Clear all tables and graphs to restart display in new mode
        self.clear_tables()

    def _configure_table_columns(self, table, stretch_column_name: str):
        """! Configure column resize behavior using column names instead of indices.
        @param table: QTableWidget instance
        @param stretch_column_name: Header text of column to stretch (e.g. "Decoded")
        """

        header = table.horizontalHeader()
        col_count = table.columnCount()

        # Build name → index map
        name_to_col = {}
        for col in range(col_count):
            item = table.horizontalHeaderItem(col)
            if item:
                name_to_col[item.text()] = col

        # Default: content-sized columns
        for col in range(col_count):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)

        # Stretch the requested column
        stretch_col = name_to_col.get(stretch_column_name)
        if stretch_col is not None:
            header.setSectionResizeMode(stretch_col, QHeaderView.Stretch)

        # Fix Count column (if present)
        count_col = name_to_col.get("Count")
        if count_col is not None:
            header.setSectionResizeMode(count_col, QHeaderView.Fixed)
            table.setColumnWidth(count_col, 70)

    def _settings_key_for_table(self, table_name: str) -> str:
        """! Generate a QSettings key for storing table column widths.
        @details
        Constructs a stable settings key namespace for persisting
        column width information for a given table. This allows
        each table (Protocol, PDO, SDO) to restore its column
        layout independently across application runs.
        @param table_name Logical name of the table (e.g. \"protocol\", \"pdo\", \"sdo\").
        @return Fully-qualified QSettings key for column width storage.
        """

        return f"tables/{table_name}/column_widths"


    def _restore_column_widths(self, table: QTableWidget, table_name: str):
        """! Restore column widths for a table from persistent settings.
        @details
        Reads previously saved column widths from QSettings and applies
        them to the given table. If no saved state exists, the function
        exits silently and leaves the default sizing unchanged.
        @note:
        Any inconsistencies (e.g. column count mismatch) are ignored
        defensively to avoid breaking the GUI on corrupted settings.
        @param table QTableWidget whose column widths should be restored.
        @param table_name Logical name used to look up saved settings.
        """

        # Retrieve stored column widths list from QSettings
        widths = self.settings.value(self._settings_key_for_table(table_name))
        if not widths:
            return

        try:
            # Apply each stored width to the corresponding column
            for col, w in enumerate(widths):
                table.setColumnWidth(col, int(w))
        except Exception:
            # Ignore malformed or incompatible stored values
            pass

    def _save_column_widths(self, table: QTableWidget, table_name: str):
        """! Persist current column widths for a table.
        @details
        Captures the current column widths of the given table and
        stores them in QSettings. These values are later restored
        during application startup to preserve user layout
        preferences.
        @param table QTableWidget whose column widths should be saved.
        @param table_name Logical name used to store settings.
        """

        # Collect current width of each column
        widths = [table.columnWidth(c) for c in range(table.columnCount())]

        # Persist widths using a table-specific settings key
        self.settings.setValue(
            self._settings_key_for_table(table_name),
            widths
        )

    def _build_left_dock(self):
        """! Build the Remote Node Control dock.
        @details
        Constructs the left-side dock widget that hosts controls
        related to remote CANopen node interaction. This includes
        actions such as network scanning and manual PDO/SDO
        transmission.
        @note
        The dock layout mirrors the logical grouping used in the
        CLI command interface but presents controls in a GUI form.
        """

        # Create the dock widget and assign a stable object name
        # (required for layout persistence)
        dock = QDockWidget("Remote Node Control", self)
        dock.setObjectName("RemoteNodeControlDock")

        # Constrain dock width to avoid excessive horizontal usage
        dock.setMinimumWidth(250)
        dock.setMaximumWidth(300)

        # Root widget for dock contents
        w = QWidget()
        l = QVBoxLayout(w)

        # Add placeholder controls for remote node operations
        for name in ("Scan Network", "Send SDO", "Receive SDO", "Send PDO"):
            # Section label
            l.addWidget(QLabel(name))
            # Corresponding action button
            l.addWidget(QPushButton(name))

        # Push controls to the top, leaving flexible space below
        l.addStretch(1)

        # Attach content widget to the dock and dock it on the left
        dock.setWidget(w)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)

    def _build_right_dock(self):
        """! Build the Bus Statistics dock.
        @details
        Constructs the right-side dock widget that presents live bus-level
        statistics and frame-rate visualizations. This dock includes:
        - A metrics table summarizing bus state
        - A frame-rate section with multi-line history graphs
        (PDO, SDO Req/Resp, Heartbeat/EMCY)
        @note
        The content and grouping mirror the information shown in the
        CLI statistics panel, adapted for graphical presentation.
        """

        # Create the dock widget with a stable object name
        # (required for layout persistence via QSettings)
        dock = QDockWidget("Bus Stats", self)
        dock.setObjectName("BusStatsDock")

        # Constrain dock width to prevent excessive horizontal usage
        dock.setMinimumWidth(300)
        dock.setMaximumWidth(600)

        # Root widget for the dock contents
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(2)
        layout.setContentsMargins(4, 4, 4, 4)

        # ------------------------------------------------------------------
        # Bus statistics table
        # ------------------------------------------------------------------
        ## Displays key/value style bus metrics (e.g. utilization,
        ## error counters, SDO status), similar to the CLI stats view.
        self.bus_table = QTableWidget(0, 2)
        self.bus_table.setHorizontalHeaderLabels(["Metric", "Value"])

        # Allow the value column to stretch and consume remaining space
        self.bus_table.horizontalHeader().setStretchLastSection(True)

        layout.addWidget(self.bus_table)

        # ------------------------------------------------------------------
        # Frame-rate visualization section
        # ------------------------------------------------------------------
        rates_label = QLabel("Frame Rates")
        rates_label.setStyleSheet("font-weight: 600;")
        layout.addWidget(rates_label)

        # ------------------------------------------------------------------
        # Multi-line rate graph widgets
        # ------------------------------------------------------------------
        ## PDO frame-rate history
        self.rate_pdo = MultiRateLineWidget(
            [("PDO", Qt.darkGreen)]
        )

        ## SDO request/response frame-rate history (shared time axis)
        self.rate_sdo = MultiRateLineWidget(
            [
                ("SDO-Req", Qt.darkBlue),
                ("SDO-Resp", Qt.darkCyan),
            ]
        )

        ## Heartbeat and EMCY frame-rate history
        self.rate_misc = MultiRateLineWidget(
            [
                ("Heartbeat", Qt.darkYellow),
                ("EMCY", Qt.darkRed),
            ]
        )

        # Add rate graph widgets to the dock layout
        layout.addWidget(self.rate_pdo)
        layout.addWidget(self.rate_sdo)
        layout.addWidget(self.rate_misc)

        # Push all content to the top, leaving flexible space below
        layout.addStretch(1)

        # Attach content widget to the dock and dock it on the right
        dock.setWidget(w)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)


    def _build_central(self):
        """! Build the central widget containing protocol, PDO, and SDO tables.
        @details
        Constructs the vertically stacked central view used to display
        decoded CANopen traffic. The central widget consists of:
        - Protocol Data table
        - PDO Data table
        - SDO Data table
        @note
        Each table is wrapped with a titled header and filter input.
        Column sizing and layout behavior are aligned with the CLI
        table presentation while leveraging Qt interaction features.
        """

        ## Create a vertical splitter to allow user-resizable sections
        self.splitter = QSplitter(Qt.Vertical)

        # ------------------------------------------------------------------
        # Create titled table blocks with per-table filters
        # ------------------------------------------------------------------
        ## Protocol data filter
        self.proto_block, self.proto_table, self.proto_filter = (
            self._make_titled_table(
                "Protocol Data",
                self._make_protocol_table()
            )
        )

        ## PDO data filter
        self.pdo_block, self.pdo_table, self.pdo_filter = (
            self._make_titled_table(
                "PDO Data",
                self._make_pdo_table()
            )
        )

        ## SDO data filter
        self.sdo_block, self.sdo_table, self.sdo_filter = (
            self._make_titled_table(
                "SDO Data",
                self._make_sdo_table()
            )
        )

        # ------------------------------------------------------------------
        # Configure column sizing behavior for each table
        # ------------------------------------------------------------------
        # One column per table is allowed to stretch to fill remaining space
        self._configure_table_columns(
            self.proto_table,
            stretch_column_name="Decoded"
        )
        self._configure_table_columns(
            self.pdo_table,
            stretch_column_name="Name"
        )
        self._configure_table_columns(
            self.sdo_table,
            stretch_column_name="Name"
        )

        # ------------------------------------------------------------------
        # Restore persisted column widths (if available)
        # ------------------------------------------------------------------
        self._restore_column_widths(self.proto_table, "protocol")
        self._restore_column_widths(self.pdo_table, "pdo")
        self._restore_column_widths(self.sdo_table, "sdo")

        # ------------------------------------------------------------------
        # Configure splitter space distribution
        # ------------------------------------------------------------------
        # Give more space to higher-volume tables (Protocol, PDO)
        self.splitter.setStretchFactor(0, 3)  # Protocol
        self.splitter.setStretchFactor(1, 4)  # PDO
        self.splitter.setStretchFactor(2, 2)  # SDO

        # Add table blocks to the splitter in display order
        self.splitter.addWidget(self.proto_block)
        self.splitter.addWidget(self.pdo_block)
        self.splitter.addWidget(self.sdo_block)

        # Install splitter as the central widget of the main window
        self.setCentralWidget(self.splitter)

    def _make_titled_table(self, title, table):
        """! Create a titled table container with an integrated filter bar.
        @details
        Wraps a QTableWidget with a title label and a text filter input.
        The filter allows interactive row filtering based on substring
        matching across all table columns. This pattern is reused for
        Protocol, PDO, and SDO tables to ensure consistent UX.
        @param title Title string displayed above the table.
        @param table QTableWidget instance to be wrapped.
        @return Tuple of (container widget, table, filter line edit).
        """

        # Container widget holding header and table
        container = QWidget()
        v = QVBoxLayout(container)

        # ------------------------------------------------------------------
        # Header row: title + filter controls
        # ------------------------------------------------------------------
        header = QHBoxLayout()

        # Title label
        lbl = QLabel(title)
        lbl.setStyleSheet("font-weight:600;")

        # Filter input for interactive row filtering
        flt = QLineEdit()
        flt.setPlaceholderText("Filter...")
        flt.textChanged.connect(
            lambda text, t=table: self._apply_filter(t, text)
        )

        # Clear button to reset filter text
        clear_btn = QPushButton("×")
        clear_btn.setFixedWidth(24)
        clear_btn.clicked.connect(flt.clear)

        # Assemble header layout
        header.addWidget(lbl)
        header.addStretch(1)
        header.addWidget(QLabel("Filter:"))
        header.addWidget(flt)
        header.addWidget(clear_btn)

        # Add header and table to container
        v.addLayout(header)
        v.addWidget(table)

        return container, table, flt

    def _make_protocol_table(self):
        """! Create and configure the Protocol Data table.
        @details
        Displays decoded CANopen protocol-level frames, including
        raw data and decoded textual representation. Column layout
        mirrors the CLI protocol table.
        """

        t = QTableWidget(0, 6)
        t.setHorizontalHeaderLabels(
            ["Time", "COB-ID", "Type", "Raw", "Decoded", "Count"]
        )
        t.setAlternatingRowColors(True)

        return t

    def _make_pdo_table(self):
        """! Create and configure the PDO Data table.
        @details
        Displays PDO-related frames with object dictionary context
        (index, subindex, name) alongside raw and decoded values.
        """

        t = QTableWidget(0, 8)
        t.setHorizontalHeaderLabels(
            ["Time", "COB-ID", "Name", "Index", "Sub", "Raw", "Decoded", "Count"]
        )
        t.setAlternatingRowColors(True)

        return t


    def _make_sdo_table(self):
        """! Create and configure the SDO Data table.
        @details
        Displays SDO request/response traffic with object dictionary
        information. Column layout intentionally matches the PDO table
        for visual consistency.
        """

        t = QTableWidget(0, 8)
        t.setHorizontalHeaderLabels(
            ["Time", "COB-ID", "Name", "Index", "Sub", "Raw", "Decoded", "Count"]
        )
        t.setAlternatingRowColors(True)

        return t

    def _apply_filter(self, table, text):
        """! Apply a substring filter to a table.
        @details
        Performs a case-insensitive substring match across all columns
        of each row. Rows that do not contain the filter text in any
        column are hidden.
        @param table QTableWidget to which the filter should be applied.
        @param text Filter text entered by the user.
        """

        # Normalize filter text for case-insensitive comparison
        text = text.lower()

        # Iterate through all rows and evaluate match condition
        for row in range(table.rowCount()):
            match = False
            for col in range(table.columnCount()):
                item = table.item(row, col)
                if item and text in item.text().lower():
                    match = True
                    break

            # Hide rows that do not match filter criteria
            table.setRowHidden(row, not match)

    def _restore_layout(self):
        """! Restore persisted window and layout state.
        @details
        Restores window geometry, dock layout, and central splitter
        state from QSettings if previously saved. Each component
        is restored independently to allow partial recovery.
        """

        if self.settings.value("geometry"):
            self.restoreGeometry(self.settings.value("geometry"))

        if self.settings.value("windowState"):
            self.restoreState(self.settings.value("windowState"))

        if self.settings.value("splitter"):
            self.splitter.restoreState(self.settings.value("splitter"))

    def _autosize_columns(self, table):
        """! Auto-size table columns and rows to fit contents.
        @details
        Triggers a one-time resize based on current cell contents.
        This is typically used after bulk updates to ensure optimal
        readability without forcing ResizeToContents permanently.
        @param table QTableWidget to be auto-sized.
        """

        table.resizeColumnsToContents()
        table.resizeRowsToContents()

    def _adjust_bus_table_height(self):
        """! Adjust the Bus Stats table height to fit all rows.
        @details
        Computes the required table height based on header height
        and the number of rows, then fixes the table height so
        that all metrics are visible without vertical scrolling.
        """

        header = self.bus_table.horizontalHeader()
        row_height = self.bus_table.verticalHeader().defaultSectionSize()
        rows = self.bus_table.rowCount()
        header_h = header.height()

        # Add small padding for table frame and margins
        total_h = header_h + rows * row_height + 6
        self.bus_table.setFixedHeight(total_h)


    def _flash_row(self, table, row):
        """! Temporarily highlight a table row to indicate activity.
        @details
        Applies a background color to all cells in the specified row
        to visually indicate new or updated data. The highlight is
        automatically cleared after a fixed timeout.
        @param table QTableWidget containing the row to highlight.
        @param row Row index to be highlighted.
        """

        # Apply highlight color to each cell in the row
        for c in range(table.columnCount()):
            item = table.item(row, c)
            if item:
                item.setBackground(self.HEAT_COLOR)

        # Schedule highlight removal after configured timeout
        QTimer.singleShot(
            self.HEAT_CLEAR_MS,
            lambda: [
                table.item(row, c).setBackground(Qt.NoBrush)
                for c in range(table.columnCount())
                if table.item(row, c)
            ]
        )

    def update_bus_stats(self):
        """! Update the Bus Statistics table and rate graphs.
        @details
        Retrieves a snapshot from the shared bus statistics backend
        and refreshes all bus-level metrics displayed in the GUI.
        This includes:
        - General bus state and node activity
        - Utilization and idle percentages
        - Aggregate and peak frame rates
        - SDO success/abort statistics and response timing
        - Error and diagnostic information
        - Top talkers and frame distribution
        - Frame-rate history graphs (PDO, SDO, Heartbeat, EMCY)
        @note
        The data and grouping mirror the CLI statistics panel.
        """

        # Retrieve an immutable snapshot of current bus statistics
        snap = self.stats.get_snapshot()

        # Clear existing rows before repopulating metrics
        self.bus_table.setRowCount(0)

        # Helper to append a metric/value row to the bus table
        def add(metric, value):
            r = self.bus_table.rowCount()
            self.bus_table.insertRow(r)
            self.bus_table.setItem(r, 0, QTableWidgetItem(metric))
            self.bus_table.setItem(r, 1, QTableWidgetItem(str(value)))

        # ------------------------------------------------------------------
        # General bus state
        # ------------------------------------------------------------------
        add("State", "Active" if snap.frame_count.total else "Idle")
        add("Active Nodes", len(snap.nodes or {}))

        # Bus utilization percentages
        util = getattr(snap.rates, "bus_util_percent", 0.0)
        add("Bus Util %", f"{util:.2f}")
        add("Bus Idle %", f"{max(0.0, 100.0 - util):.2f}")

        # ------------------------------------------------------------------
        # Frame-rate summary
        # ------------------------------------------------------------------
        rates = snap.rates.latest
        add("Total FPS", f"{rates.get('total', 0.0):.1f}")
        add("Peak FPS", f"{snap.rates.peak_fps:.1f}")

        # ------------------------------------------------------------------
        # SDO statistics
        # ------------------------------------------------------------------
        add("SDO OK / Abort", f"{snap.sdo.success}/{snap.sdo.abort}")

        if snap.sdo.response_time:
            avg_rt = sum(snap.sdo.response_time) / len(snap.sdo.response_time)
            add("Avg SDO Resp (ms)", f"{avg_rt * 1000:.1f}")
        else:
            add("Avg SDO Resp (ms)", "-")

        # ------------------------------------------------------------------
        # Error diagnostics
        # ------------------------------------------------------------------
        if snap.error.last_time or snap.error.last_frame:
            add(
                "Last Error Frame",
                f"[{snap.error.last_time}] {snap.error.last_frame}"
            )
        else:
            add("Last Error Frame", "-")

        # ------------------------------------------------------------------
        # Top talkers (most active COB-IDs)
        # ------------------------------------------------------------------
        top = snap.top_talkers.most_common(analyzer_defs.MAX_STATS_SHOW)
        add(
            "Top Talkers",
            ", ".join(f"0x{c:03X}:{n}" for c, n in top) if top else "-"
        )

        # ------------------------------------------------------------------
        # Frame distribution by type
        # ------------------------------------------------------------------
        dist = sorted(
            ((k.name, v) for k, v in snap.frame_count.counts.items()),
            key=lambda kv: kv[1],
            reverse=True
        )
        add(
            "Frame Dist.",
            ", ".join(f"{k}:{v}" for k, v in dist[:analyzer_defs.MAX_STATS_SHOW])
            if dist else "-"
        )

        # ------------------------------------------------------------------
        # Update frame-rate history graphs
        # ------------------------------------------------------------------
        hist = snap.rates.history

        # PDO rate graph
        self.rate_pdo.update(
            {"PDO": rates.get("pdo", 0.0)},
            {"PDO": hist.get("pdo", [])}
        )

        # SDO request/response rate graph
        self.rate_sdo.update(
            {
                "SDO-Req": rates.get("sdo_req", 0.0),
                "SDO-Resp": rates.get("sdo_res", 0.0),
            },
            {
                "SDO-Req": hist.get("sdo_req", []),
                "SDO-Resp": hist.get("sdo_res", []),
            }
        )

        # Heartbeat and EMCY rate graph
        self.rate_misc.update(
            {
                "Heartbeat": rates.get("hb", 0.0),
                "EMCY": rates.get("emcy", 0.0),
            },
            {
                "Heartbeat": hist.get("hb", []),
                "EMCY": hist.get("emcy", []),
            }
        )

        # ------------------------------------------------------------------
        # Adjust bus table column sizing
        # ------------------------------------------------------------------
        header = self.bus_table.horizontalHeader()

        # Metric column: size to longest metric name
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)

        # Value column: occupy remaining horizontal space
        header.setSectionResizeMode(1, QHeaderView.Stretch)

        # Lock metric column width after sizing
        w = self.bus_table.columnWidth(0)
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        self.bus_table.setColumnWidth(0, w)

        # Adjust table height so all rows are visible without scrolling
        self._adjust_bus_table_height()

    def update_table(self, table, fixed_map, key, values):
        """! Insert or update a row in a data table.
        @details
        Updates the specified table based on the current display mode:
        - Fixed mode: rows are aggregated by key and counts incremented
        - Sequential mode: each frame is appended as a new row
        @note
        Row highlighting is applied to indicate recent activity.
        @param table Target QTableWidget.
        @param fixed_map Mapping of aggregation keys to table rows.
        @param key Unique key identifying a row in Fixed mode.
        @param values List of column values to insert/update.
        """

        # Index of the count column (last column)
        count_col = table.columnCount() - 1

        if self.fixed:
            # Fixed (aggregated) mode
            row = fixed_map.get(key)
            if row is None:
                # First occurrence of this key
                row = table.rowCount()
                fixed_map[key] = row
                table.insertRow(row)
                for c, v in enumerate(values):
                    table.setItem(row, c, QTableWidgetItem(str(v)))
            else:
                # Increment frame count for existing row
                item = table.item(row, count_col)
                if item is None:
                    item = QTableWidgetItem("1")
                    table.setItem(row, count_col, item)
                else:
                    item.setText(str(int(item.text()) + 1))

            # Highlight updated row
            self._flash_row(table, row)
        else:
            # Sequential (rolling) mode
            row = table.rowCount()
            table.insertRow(row)
            for c, v in enumerate(values):
                table.setItem(row, c, QTableWidgetItem(str(v)))

            # Highlight newly added row
            self._flash_row(table, row)

            # Enforce maximum table height by removing oldest rows
            if row > analyzer_defs.DATA_TABLE_HEIGHT:
                table.removeRow(0)

    def clear_tables(self):
        """! Clear all displayed data and reset statistics.
        @details
        Clears protocol, PDO, and SDO tables, resets fixed-mode
        aggregation maps, clears bus statistics and rate graphs,
        and resets the backend statistics counters.
        """

        # ------------------------------------------------------------------
        # Clear data tables
        # ------------------------------------------------------------------
        for t in (self.proto_table, self.pdo_table, self.sdo_table):
            t.setRowCount(0)

        self.fixed_proto.clear()
        self.fixed_pdo.clear()
        self.fixed_sdo.clear()

        # ------------------------------------------------------------------
        # Clear bus statistics table
        # ------------------------------------------------------------------
        self.bus_table.setRowCount(0)

        # ------------------------------------------------------------------
        # Clear frame-rate graphs
        # ------------------------------------------------------------------
        if hasattr(self, "rate_pdo"):
            self.rate_pdo.clear()
        if hasattr(self, "rate_sdo"):
            self.rate_sdo.clear()
        if hasattr(self, "rate_misc"):
            self.rate_misc.clear()

        # Reset backend bus statistics counters
        self.stats.reset()

    def on_frame(self, p):
        """! Handle a newly decoded CAN frame.
        @details
        Called from the GUI worker thread via signal emission.
        Dispatches the decoded frame to the appropriate table
        (Protocol, PDO, or SDO), updates row counts, applies
        highlighting, and refreshes bus statistics.
        @param p Dictionary containing decoded CAN frame fields.
        """

        try:
            # Extract common frame fields
            t = p.get("time")
            cob = f"0x{p['cob']:03X}"
            raw = p.get("raw", "")
            dec = p.get("decoded", "")
            cnt = 1

            ftype = p.get("type")
            name = ftype.name if hasattr(ftype, "name") else str(ftype)

            # Dispatch frame based on type
            if ftype == analyzer_defs.frame_type.PDO:
                key = (p["cob"], p["index"], p["sub"])
                self.update_table(
                    self.pdo_table, self.fixed_pdo, key,
                    [
                        t, cob, p.get("name"),
                        f"0x{p['index']:04X}", f"0x{p['sub']:02X}",
                        raw, dec, cnt
                    ]
                )
            elif ftype in (
                analyzer_defs.frame_type.SDO_REQ,
                analyzer_defs.frame_type.SDO_RES
            ):
                key = (p["cob"], p["index"], p["sub"])
                self.update_table(
                    self.sdo_table, self.fixed_sdo, key,
                    [
                        t, cob, p.get("name"),
                        f"0x{p['index']:04X}", f"0x{p['sub']:02X}",
                        raw, dec, cnt
                    ]
                )
            else:
                # Protocol or miscellaneous frame
                key = (p["cob"], name)
                self.update_table(
                    self.proto_table, self.fixed_proto, key,
                    [t, cob, name, raw, dec, cnt]
                )

            # Refresh bus statistics after processing frame
            self.update_bus_stats()
        except Exception as e:
            # Ignore interruptions during shutdown
            if isinstance(e, KeyboardInterrupt):
                return
            raise


    def closeEvent(self, event):
        """! Handle application shutdown and cleanup.
        @details
        Ensures that background worker threads are stopped
        cleanly and that window layout state is persisted
        before exiting the application.
        @param event Qt close event.
        """

        # ------------------------------------------------------------------
        # Stop worker thread cleanly
        # ------------------------------------------------------------------
        if hasattr(self, "worker"):
            self.worker.stop()

        if hasattr(self, "thread"):
            self.thread.quit()
            self.thread.wait()

        # ------------------------------------------------------------------
        # Persist layout and geometry state
        # ------------------------------------------------------------------
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("windowState", self.saveState())
        self.settings.setValue("splitter", self.splitter.saveState())

        super().closeEvent(event)


def display_gui(stats, processed_frame, fixed=False):
    """! Launch the CANopen Analyzer GUI application.
    @details
    Creates the Qt application instance, initializes the main
    window, and starts a background worker thread to deliver
    decoded CAN frames to the GUI. This function also installs
    proper Ctrl+C (SIGINT) handling so that the application
    shuts down cleanly when run from a terminal.
    @note
    The worker thread communicates with the GUI exclusively
    via Qt signals to ensure thread safety.
    @param stats Shared bus statistics object used by the GUI.
    @param processed_frame Thread-safe queue containing decoded
                            CAN frames from the backend.
    @param fixed If True, start in Fixed (aggregated) display mode;
                 otherwise start in Sequential mode.
    """

    # ------------------------------------------------------------------
    # Qt application and main window initialization
    # ------------------------------------------------------------------
    app = QApplication(sys.argv)
    win = CANopenMainWindow(stats, fixed)

    # ------------------------------------------------------------------
    # Worker thread setup
    # ------------------------------------------------------------------
    # Create the GUI worker and its dedicated QThread.
    # The worker continuously consumes decoded CAN frames
    # from the processed_frame queue and emits them to the GUI.
    win.worker = GUIUpdateWorker(processed_frame)
    win.thread = QThread()

    # Move worker object to the background thread
    win.worker.moveToThread(win.thread)

    # Start worker loop when thread starts
    win.thread.started.connect(win.worker.run)

    # Deliver decoded frames to the GUI thread
    win.worker.frame_received.connect(win.on_frame)

    # Start the worker thread
    win.thread.start()

    # ------------------------------------------------------------------
    # Proper Ctrl+C (SIGINT) handling
    # ------------------------------------------------------------------
    # Ensure that Ctrl+C from the terminal triggers a clean
    # Qt shutdown, which in turn invokes closeEvent() and
    # stops background threads safely.
    def handle_sigint(*_):
        QTimer.singleShot(0, app.quit)

    signal.signal(signal.SIGINT, handle_sigint)

    # Show the main window and enter the Qt event loop
    win.show()
    sys.exit(app.exec())
