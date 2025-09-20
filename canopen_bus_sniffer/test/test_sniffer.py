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

"""

import io
import json
import tempfile
import os
import pytest
from unittest import mock
from PyQt5 import QtWidgets
import can
import time

import canopen_sniffer_gui as sniffer


@pytest.fixture
def app(qtbot):
    """Qt Application fixture for GUI tests."""
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def mainwindow(app, tmp_path):
    """Create main window with mocked CAN bus."""
    w = sniffer.MainWindow(eds_path=None, channel="vcan0")
    w.worker.stop()  # stop worker thread for testing
    return w


# ----------------- SDO -----------------

def test_send_sdo_valid(mainwindow, monkeypatch):
    sent_msgs = []

    class DummyBus:
        def __init__(self, *a, **kw): pass
        def send(self, msg): sent_msgs.append(msg)

    monkeypatch.setattr(sniffer.can.interface, "Bus", DummyBus)

    mainwindow.sdo_send_node.setText("0x02")
    mainwindow.sdo_index_edit.setText("0x6000")
    mainwindow.sdo_sub_edit.setText("0")
    mainwindow.sdo_value_edit.setText("5")
    mainwindow.sdo_size_combo.setCurrentText("1")

    # patch messagebox
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", lambda *a, **k: None)

    mainwindow.on_send_sdo()
    assert len(sent_msgs) == 1
    assert sent_msgs[0].arbitration_id == 0x600 + 0x02


def test_recv_sdo_valid(mainwindow, monkeypatch):
    sent_msgs = []

    class DummyBus:
        def __init__(self, *a, **kw): pass
        def send(self, msg): sent_msgs.append(msg)

    monkeypatch.setattr(sniffer.can.interface, "Bus", DummyBus)
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", lambda *a, **k: None)

    mainwindow.sdo_recv_node.setText("0x02")
    mainwindow.sdo_recv_index.setText("0x6000")
    mainwindow.sdo_recv_sub.setText("0x01")

    mainwindow.on_recv_sdo()
    assert len(sent_msgs) == 1
    assert sent_msgs[0].arbitration_id == 0x600 + 0x02


# ----------------- PDO -----------------

def test_send_pdo_once(mainwindow, monkeypatch):
    sent_msgs = []

    class DummyBus:
        def __init__(self, *a, **kw): pass
        def send(self, msg): sent_msgs.append(msg)

    monkeypatch.setattr(sniffer.can.interface, "Bus", DummyBus)

    mainwindow.pdo_cob_edit.setText("0x181")
    mainwindow.pdo_data_edit.setText("01 02 03 04")

    mainwindow.on_send_pdo()
    assert len(sent_msgs) == 1
    assert sent_msgs[0].arbitration_id == 0x181
    assert sent_msgs[0].data[:4] == bytes([1,2,3,4])


# ----------------- Export -----------------

def test_export_csv_json_hist(mainwindow, monkeypatch, tmp_path):
    # Fill buffer with fake frame
    frame = {
        "time": "12:00:00",
        "node": "1",
        "cob": 0x181,
        "type": "PDO",
        "name": "Test",
        "index_list": ["0x6000"],
        "sub_list": ["0x01"],
        "dtype": "UNSIGNED8",
        "raw": "01",
        "decoded": "1"
    }
    mainwindow.buffer_frames.append(frame)
    mainwindow.insert_or_update_row(frame)

    # patch file dialogs
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
        lambda *a, **k: (str(tmp_path/"out.csv"), None))
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", lambda *a, **k: None)

    # --- helper to always click "Yes"
    def force_yes(monkeypatch):
        yes_button_store = {}
        orig_add_button = QtWidgets.QMessageBox.addButton

        def fake_add_button(self, *a, **k):
            btn = orig_add_button(self, *a, **k)
            if "btn" not in yes_button_store:
                yes_button_store["btn"] = btn
            return btn

        monkeypatch.setattr(QtWidgets.QMessageBox, "addButton", fake_add_button)
        monkeypatch.setattr(QtWidgets.QMessageBox, "exec_", lambda self: None)
        monkeypatch.setattr(QtWidgets.QMessageBox, "clickedButton",
            lambda self: yes_button_store.get("btn"))

    # Export CSV
    force_yes(monkeypatch)
    mainwindow.export_csv_dialog()
    csv_path = tmp_path / "out.csv"
    assert csv_path.exists(), "CSV file was not created"

    # Export JSON
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
        lambda *a, **k: (str(tmp_path/"out.json"), None))
    force_yes(monkeypatch)
    mainwindow.export_json()
    json_path = tmp_path / "out.json"
    assert json_path.exists(), "JSON file was not created"
    data = json.loads(json_path.read_text())
    assert data[0]["name"] == "Test"

    # Export Histogram CSV
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
        lambda *a, **k: (str(tmp_path/"hist.csv"), None))
    mainwindow.hist.push(time.time(), 0x181)
    mainwindow.export_hist_csv()
    assert (tmp_path/"hist.csv").exists()


def test_export_pcap_not_available(mainwindow, monkeypatch):
    monkeypatch.setattr(sniffer, "PcapWriter", None)
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", lambda *a, **k: ("out.pcap", None))
    mainwindow.export_pcap()  # should not crash


# ----------------- Misc -----------------

def test_toggle_pause(mainwindow):
    assert not mainwindow.pause
    mainwindow.toggle_pause()
    assert mainwindow.pause
    mainwindow.toggle_pause()
    assert not mainwindow.pause


def test_clear_table(mainwindow):
    mainwindow.buffer_frames.append({"cob":0x181,"decoded":"test"})
    mainwindow.table.insertRow(0)
    mainwindow.sdo_table.insertRow(0)
    mainwindow.clear_table()
    assert mainwindow.table.rowCount() == 0
    assert mainwindow.sdo_table.rowCount() == 0
    assert not mainwindow.buffer_frames
