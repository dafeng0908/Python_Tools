import sys
import time
import csv
import os
import ctypes
import struct
import math
from dataclasses import dataclass, field
from collections import deque
from enum import Enum, auto
from typing import Deque, Dict, List, Tuple, Optional, Set, Callable

from PySide6.QtCore import Qt, QThread, Signal, Slot, QTimer, QDateTime, QPointF, QSignalBlocker, QObject
from PySide6.QtGui import QFont, QAction
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QComboBox, QPlainTextEdit, QLineEdit, QTabWidget,
    QGroupBox, QMessageBox, QFileDialog, QCheckBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QSpinBox, QSplitter, QSlider
)
from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis
from PySide6.QtGui import QPainter

try:
    from PCANBasic import PCANBasic, TPCANMsg
except Exception as e:
    raise RuntimeError(
        "無法匯入 PCANBasic.py。請確認 main.py 與 PCANBasic.py 在同一資料夾。\n"
        f"錯誤：{e}"
    )


def to_int(v) -> int:
    if v is None:
        raise ValueError("value is None")
    if isinstance(v, int):
        return v
    if isinstance(v, (bytes, bytearray)):
        return int.from_bytes(v, "little", signed=False)
    try:
        if isinstance(v, ctypes._SimpleCData):
            return int(v.value)
    except Exception:
        pass
    if isinstance(v, str):
        s = v.strip()
        if s.lower().startswith("0x"):
            return int(s, 16)
        return int(s, 10)
    return int(v)


def pcan_const(name: str, default=None):
    import PCANBasic as PB
    return getattr(PB, name, default)


PCAN_ERROR_OK = to_int(pcan_const("PCAN_ERROR_OK", 0x00000))
PCAN_ERROR_BUSOFF = to_int(pcan_const("PCAN_ERROR_BUSOFF", 0x00010))
PCAN_MESSAGE_STANDARD = to_int(pcan_const("PCAN_MESSAGE_STANDARD", 0x00))
PCAN_MESSAGE_EXTENDED = to_int(pcan_const("PCAN_MESSAGE_EXTENDED", 0x02))

BAUD_MAP = {
    "1M": to_int(pcan_const("PCAN_BAUD_1M", 0x0014)),
    "800K": to_int(pcan_const("PCAN_BAUD_800K", 0x0016)),
    "500K": to_int(pcan_const("PCAN_BAUD_500K", 0x001C)),
    "250K": to_int(pcan_const("PCAN_BAUD_250K", 0x011C)),
    "125K": to_int(pcan_const("PCAN_BAUD_125K", 0x031C)),
    "100K": to_int(pcan_const("PCAN_BAUD_100K", 0x432F)),
    "50K": to_int(pcan_const("PCAN_BAUD_50K", 0x472F)),
    "20K": to_int(pcan_const("PCAN_BAUD_20K", 0x532F)),
    "10K": to_int(pcan_const("PCAN_BAUD_10K", 0x672F)),
    "5K": to_int(pcan_const("PCAN_BAUD_5K", 0x7F7F)),
}


def build_usb_channels():
    chans = []
    for i in range(1, 17):
        name = f"PCAN_USBBUS{i}"
        val = pcan_const(name, None)
        if val is not None:
            chans.append((f"USBBUS{i}", to_int(val)))
    if not chans:
        chans.append(("USBBUS1", to_int(pcan_const("PCAN_USBBUS1", 0x51))))
    return chans


def detect_available_pcan_channels(pcan_obj=None):
    """
    Detect currently attached PCAN USB channels.

    Uses PCANBasic.GetValue(channel, PCAN_CHANNEL_CONDITION) when available.
    If unsupported by the PCANBasic.py/DLL version, falls back to all known USB channels.
    """
    pcan = pcan_obj or PCANBasic()
    all_chans = build_usb_channels()

    cond_param = pcan_const("PCAN_CHANNEL_CONDITION", None)
    available_flag = to_int(pcan_const("PCAN_CHANNEL_AVAILABLE", 0x01))
    occupied_flag = to_int(pcan_const("PCAN_CHANNEL_OCCUPIED", 0x02))

    if cond_param is None or not hasattr(pcan, "GetValue"):
        return all_chans

    detected = []
    for name, ch in all_chans:
        try:
            result = pcan.GetValue(ch, cond_param)
            if not (isinstance(result, tuple) and len(result) >= 2):
                continue
            status = to_int(result[0])
            condition = to_int(result[1])
            if status == PCAN_ERROR_OK and (condition & (available_flag | occupied_flag)):
                suffix = " (occupied)" if (condition & occupied_flag) else " (available)"
                detected.append((f"{name}{suffix}", ch))
        except Exception:
            continue

    return detected


TX_REQ_ID = 0x00000752
RX_RSP_ID = 0x00000753

RSOC_TX = bytes([0x05, 0x00])
R0_TX = bytes([0x41, 0x02])
EKF_SOC_CODES = [(f"ekf_soc{i:02d}", 0x2F + i) for i in range(1, 17)]
EKF_R0_CODES = [(f"ekf_r0_{i:02d}", 0x2F + i) for i in range(1, 17)]
SIGNALS_ALL = ["rsoc"] + [n for n, _ in EKF_SOC_CODES] + ["r0"] + [n for n, _ in EKF_R0_CODES]
CODE_TO_SOC = {code: name for name, code in EKF_SOC_CODES}
CODE_TO_R0 = {code: name for name, code in EKF_R0_CODES}

PAGE3_FIELDS = [
    ("soc", 0x50), ("soc_true", 0x51), ("p00", 0x52), ("p11", 0x53),
    ("p01", 0x54), ("q00", 0x55), ("q11", 0x56), ("R", 0x57),
    ("innovation", 0x58), ("S", 0x59), ("k0", 0x5A), ("k1", 0x5B),
]
PAGE3_FIELD_BY_CODE = {code: name for name, code in PAGE3_FIELDS}
PAGE3_HEADERS = ["time"] + [name for name, _ in PAGE3_FIELDS]


def u16_le(lo: int, hi: int) -> int:
    return ((hi & 0xFF) << 8) | (lo & 0xFF)


@dataclass
class CanFrame:
    ts_ms: int
    can_id: int
    dlc: int
    data: bytes
    msgtype: int


class PcanRxThread(QThread):
    frame_received = Signal(object)
    error_happened = Signal(str)

    def __init__(self, pcan: PCANBasic, channel_val: int, parent=None):
        super().__init__(parent)
        self._pcan = pcan
        self._channel = to_int(channel_val)
        self._running = False

    def start_rx(self):
        self._running = True
        self.start()

    def stop_rx(self):
        self._running = False
        self.wait(800)

    def run(self):
        while self._running:
            try:
                res = self._pcan.Read(self._channel)
                if not (isinstance(res, tuple) and len(res) >= 3):
                    time.sleep(0.002)
                    continue
                status, msg, ts = res[0], res[1], res[2]
                if to_int(status) != PCAN_ERROR_OK:
                    time.sleep(0.001)
                    continue

                can_id = to_int(msg.ID)
                dlc = to_int(msg.LEN)
                msgtype = to_int(msg.MSGTYPE)
                data = bytes(to_int(msg.DATA[i]) & 0xFF for i in range(min(dlc, 8)))

                try:
                    ts_ms = to_int(getattr(ts, "millis", 0)) + to_int(getattr(ts, "micros", 0)) // 1000
                except Exception:
                    ts_ms = int(time.time() * 1000)

                self.frame_received.emit(CanFrame(ts_ms, can_id, dlc, data, msgtype))
            except Exception as e:
                self.error_happened.emit(f"RX 讀取錯誤：{e}")
                time.sleep(0.05)


class CsvAutoSplitWriter:
    def __init__(self, base_path: str, headers: List[str], max_bytes: int = 100 * 1024 * 1024):
        self.base_path = base_path
        self.headers = headers
        self.max_bytes = max_bytes
        self.part_idx = 0
        self.file = None
        self.writer = None

    def _make_part_path(self, idx: int) -> str:
        root, ext = os.path.splitext(self.base_path)
        if not ext:
            ext = ".csv"
        return f"{root}_{idx:03d}{ext}"

    def _open_new_part(self):
        self.close()
        self.file = open(self._make_part_path(self.part_idx), "w", newline="", encoding="utf-8-sig")
        self.writer = csv.DictWriter(self.file, fieldnames=self.headers)
        self.writer.writeheader()

    def write_row(self, row: Dict[str, object]):
        if self.file is None:
            self._open_new_part()
        if self.file.tell() >= self.max_bytes:
            self.part_idx += 1
            self._open_new_part()
        self.writer.writerow({h: row.get(h, "") for h in self.headers})
        if self.file.tell() >= self.max_bytes:
            self.part_idx += 1
            self._open_new_part()

    def close(self):
        if self.file:
            try:
                self.file.flush()
                self.file.close()
            except Exception:
                pass
        self.file = None
        self.writer = None


class Page3State(Enum):
    IDLE = auto()
    START_ROUND = auto()
    SEND_ONE = auto()
    WAIT_RX = auto()
    WAIT_10MS = auto()
    ROUND_DONE = auto()


@dataclass
class Page3Row:
    round_index: int
    time_label: str
    values: Dict[str, float] = field(default_factory=dict)

    def to_csv_dict(self) -> Dict[str, object]:
        row = {"time": self.time_label}
        row.update(self.values)
        return row


class Page3Controller(QObject):
    state_changed = Signal(str)
    row_started = Signal(int, str)
    row_updated = Signal(int, dict)
    row_finished = Signal(int, dict)
    distribution_updated = Signal(dict)
    log_message = Signal(str)
    tx_frame = Signal(int, bytes, bool)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)

        self.state = Page3State.IDLE
        self.round_index = 0
        self.field_index = 0
        self.current_code: Optional[int] = None
        self.current_name: Optional[str] = None

        self.round_start_time: Optional[float] = None
        self.round_finish_time: Optional[float] = None
        self.next_round_time: Optional[float] = None

        self.rows: List[Page3Row] = []
        self.current_row: Optional[Page3Row] = None
        self.record_csv_enabled = False

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1)
        self._tick_timer.timeout.connect(self._tick)

        self._wait10_timer = QTimer(self)
        self._wait10_timer.setSingleShot(True)
        self._wait10_timer.timeout.connect(self._on_wait10_done)

    def start(self) -> None:
        if self.state != Page3State.IDLE:
            return
        self._set_state(Page3State.START_ROUND)
        self._tick_timer.start()

    def stop(self) -> None:
        self._tick_timer.stop()
        self._wait10_timer.stop()
        self._set_state(Page3State.IDLE)
        self.current_code = None
        self.current_name = None
        self.next_round_time = None

    def clear(self) -> None:
        self.stop()
        self.round_index = 0
        self.field_index = 0
        self.round_start_time = None
        self.round_finish_time = None
        self.rows.clear()
        self.current_row = None

    def set_record_csv(self, enabled: bool) -> None:
        self.record_csv_enabled = enabled

    def export_rows(self) -> List[Dict[str, object]]:
        return [r.to_csv_dict() for r in self.rows]

    def get_row(self, index: int) -> Optional[Page3Row]:
        if 0 <= index < len(self.rows):
            return self.rows[index]
        return None

    def on_rx_frame(self, can_id: int, data: bytes) -> None:
        if self.state != Page3State.WAIT_RX:
            return
        if can_id != RX_RSP_ID:
            return
        if len(data) < 6:
            return

        rx_code = data[0]
        rx_sub = data[1]
        if self.current_code is None or self.current_name is None:
            return
        if rx_code != self.current_code or rx_sub != 0x02:
            return

        value = struct.unpack("<f", bytes(data[2:6]))[0]
        if self.current_row is None:
            return

        self.current_row.values[self.current_name] = float(value)
        self.row_updated.emit(self.current_row.round_index, dict(self.current_row.values))
        self.distribution_updated.emit(dict(self.current_row.values))
        self.log_message.emit(f"RX matched: 0x{rx_code:02X} -> {self.current_name}={value:.6f}")

        self.current_code = None
        self.current_name = None
        self._set_state(Page3State.WAIT_10MS)
        self._wait10_timer.start(10)

    def _tick(self) -> None:
        now = time.perf_counter()

        if self.state == Page3State.START_ROUND:
            self._begin_round(now)
            return

        if self.state == Page3State.SEND_ONE:
            self._send_one()
            return

        if self.state == Page3State.ROUND_DONE:
            if self.next_round_time is not None and now >= self.next_round_time:
                self._set_state(Page3State.START_ROUND)

    def _begin_round(self, now: float) -> None:
        self.field_index = 0
        self.round_start_time = now
        self.round_finish_time = None

        self.current_row = Page3Row(
            round_index=self.round_index,
            time_label=f"{self.round_index}.00",
        )
        self.rows.append(self.current_row)
        self.row_started.emit(self.current_row.round_index, self.current_row.time_label)
        self.log_message.emit(f"Round {self.round_index} started")
        self._set_state(Page3State.SEND_ONE)

    def _send_one(self) -> None:
        if self.current_row is None:
            self._set_state(Page3State.IDLE)
            return

        if self.field_index >= len(PAGE3_FIELDS):
            self._finish_round()
            return

        name, code = PAGE3_FIELDS[self.field_index]
        payload = bytes([code, 0x02])

        self.current_name = name
        self.current_code = code
        self.tx_frame.emit(TX_REQ_ID, payload, True)
        self.log_message.emit(f"TX sent: 0x{code:02X} -> {name}")
        self._set_state(Page3State.WAIT_RX)

    def _on_wait10_done(self) -> None:
        if self.state != Page3State.WAIT_10MS:
            return

        self.field_index += 1
        if self.field_index >= len(PAGE3_FIELDS):
            self._finish_round()
        else:
            self._set_state(Page3State.SEND_ONE)

    def _finish_round(self) -> None:
        now = time.perf_counter()
        self.round_finish_time = now

        if self.current_row is not None:
            self.row_finished.emit(self.current_row.round_index, dict(self.current_row.values))
            self.distribution_updated.emit(dict(self.current_row.values))

        if self.round_start_time is None:
            base = now
        else:
            base = max(self.round_start_time + 1.0, now)

        self.next_round_time = base
        self.log_message.emit(
            f"Round {self.round_index} finished; next round after {max(0.0, base-now):.3f}s"
        )

        self.round_index += 1
        self._set_state(Page3State.ROUND_DONE)

    def _set_state(self, state: Page3State) -> None:
        self.state = state
        self.state_changed.emit(state.name)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PCAN Qt6 UI - v9")
        self.resize(1560, 1020)

        self.pcan = PCANBasic()
        self.connected = False
        self.channel_val = 0
        self.rx_thread = None

        self._frame_queue: Deque[CanFrame] = deque(maxlen=120000)
        self._raw_queue: Deque[Tuple[str, bool]] = deque(maxlen=120000)
        self._id_filter_set: Optional[Set[int]] = None

        self._ui_flush_timer = QTimer(self)
        self._ui_flush_timer.setInterval(50)
        self._ui_flush_timer.timeout.connect(self.flush_ui)

        self._chart_timer = QTimer(self)
        self._chart_timer.setInterval(200)
        self._chart_timer.timeout.connect(self.refresh_chart)

        self._page3_chart_timer = QTimer(self)
        self._page3_chart_timer.setInterval(200)
        self._page3_chart_timer.timeout.connect(self.refresh_page3_chart)

        self.cmd_periodic = [QTimer(self), QTimer(self), QTimer(self)]
        for t in self.cmd_periodic:
            t.setInterval(1000)

        self.page2_periodic = QTimer(self)
        self.page2_periodic.setInterval(1000)

        self._burst_queue: Deque[Tuple[str, bytes]] = deque()
        self._burst_timer = QTimer(self)
        self._burst_timer.setInterval(10)
        self._burst_timer.timeout.connect(self._burst_send_next)

        self.max_points = 20000
        self.data_buf = {k: deque(maxlen=self.max_points) for k in SIGNALS_ALL}
        self.latest_value: Dict[str, float] = {}
        self.csv_rows: List[Dict[str, object]] = []

        self.page3_controller = Page3Controller(self)
        self.page3_last_values: Dict[str, float] = {}
        self.page3_selected_index: int = -1
        self._page3_last_tx_perf: Optional[float] = None

        self.page4_data_rows: List[Dict[str, object]] = []
        self.page4_source_name = ""

        self.series_map = {}
        self._t0 = time.monotonic()
        self._max_t_seen = 0.0
        self._bulk_checking = False
        self._waiting_first_sample_after_clear = False

        self._build_ui()
        self._wire()

        self._ui_flush_timer.start()
        self._chart_timer.start()
        self._page3_chart_timer.start()

    def _build_ui(self):
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.page1 = QWidget()
        self.page2 = QWidget()
        self.page3 = QWidget()
        self.page4 = QWidget()

        self.tabs.addTab(self.page1, "Page1")
        self.tabs.addTab(self.page2, "Page2")
        self.tabs.addTab(self.page3, "Page3")
        self.tabs.addTab(self.page4, "Page4")

        self._build_page1()
        self._build_page2()
        self._build_page3()
        self._build_page4()

        act_export = QAction("匯出 CSV", self)
        act_export.triggered.connect(self.export_csv)
        self.menuBar().addMenu("File").addAction(act_export)

    def _build_page1(self):
        layout = QVBoxLayout(self.page1)
        g_conn = QGroupBox("連線參數")
        gl = QGridLayout(g_conn)

        self.cmb_channel = QComboBox()
        self.btn_refresh_channels = QPushButton("重新偵測")
        self.refresh_pcan_channels()

        self.cmb_baud = QComboBox()
        for k, v in BAUD_MAP.items():
            self.cmb_baud.addItem(k, v)
        self.cmb_baud.setCurrentText("250K")

        self.cmb_txframe = QComboBox()
        self.cmb_txframe.addItems(["STD", "EXT"])
        self.cmb_txframe.setCurrentText("EXT")

        self.btn_connect = QPushButton("連線")
        self.lbl_status = QLabel("Status: Disconnected")
        self.lbl_status.setStyleSheet("font-weight:600;")

        gl.addWidget(QLabel("Channel"), 0, 0)
        gl.addWidget(self.cmb_channel, 0, 1)
        gl.addWidget(self.btn_refresh_channels, 0, 2)
        gl.addWidget(QLabel("Baud"), 0, 3)
        gl.addWidget(self.cmb_baud, 0, 4)
        gl.addWidget(QLabel("Tx Frame"), 0, 5)
        gl.addWidget(self.cmb_txframe, 0, 6)
        gl.addWidget(self.btn_connect, 0, 7)
        gl.addWidget(self.lbl_status, 0, 8)
        layout.addWidget(g_conn)

        g_raw = QGroupBox("CAN RAW (TX/RX) + Filters")
        vraw = QVBoxLayout(g_raw)
        ctrl = QHBoxLayout()

        self.chk_show_std = QCheckBox("STD")
        self.chk_show_ext = QCheckBox("EXT")
        self.chk_show_std.setChecked(False)
        self.chk_show_ext.setChecked(True)

        self.le_id_filter = QLineEdit("")
        self.le_id_filter.setPlaceholderText("ID filter(hex), e.g. 753 or 0x753 or 0x752,0x753")
        self.le_id_filter.setMaximumWidth(380)
        self.btn_apply_filter = QPushButton("Apply")
        self.btn_clear_filter = QPushButton("Clear Filter")

        self.btn_pause = QPushButton("Pause")
        self.btn_pause.setCheckable(True)
        self.btn_clear = QPushButton("Clear")

        ctrl.addWidget(QLabel("Show:"))
        ctrl.addWidget(self.chk_show_std)
        ctrl.addWidget(self.chk_show_ext)
        ctrl.addSpacing(12)
        ctrl.addWidget(QLabel("ID Filter:"))
        ctrl.addWidget(self.le_id_filter)
        ctrl.addWidget(self.btn_apply_filter)
        ctrl.addWidget(self.btn_clear_filter)
        ctrl.addSpacing(12)
        ctrl.addWidget(self.btn_pause)
        ctrl.addWidget(self.btn_clear)
        ctrl.addStretch(1)

        self.txt_raw = QPlainTextEdit()
        self.txt_raw.setReadOnly(True)
        self.txt_raw.setMaximumBlockCount(12000)
        font = QFont("Consolas")
        font.setPointSize(10)
        self.txt_raw.setFont(font)

        vraw.addLayout(ctrl)
        vraw.addWidget(self.txt_raw)
        layout.addWidget(g_raw, stretch=2)

        g_cmd = QGroupBox("Commands (3 groups)")
        grid = QGridLayout(g_cmd)
        self.cmd_id = []
        self.cmd_len = []
        self.cmd_data = []
        self.cmd_is_ext = []
        self.btn_once = []
        self.btn_periodic = []
        defaults = [
            ("00000752", "EXT", 2, "70 00"),
            ("00000752", "EXT", 2, "71 00"),
            ("00000752", "EXT", 2, "72 00"),
        ]
        for i, (did, dfrm, dlen, ddat) in enumerate(defaults):
            le_id = QLineEdit(did)
            le_id.setMaximumWidth(140)
            cb_frame = QComboBox()
            cb_frame.addItems(["STD", "EXT"])
            cb_frame.setCurrentText(dfrm)
            sp_len = QSpinBox()
            sp_len.setRange(0, 8)
            sp_len.setValue(dlen)
            le_data = QLineEdit(ddat)
            le_data.setPlaceholderText("例如: 70 00")
            b_once = QPushButton("單次送出")
            b_per = QPushButton("1s 週期送出")
            b_per.setCheckable(True)

            self.cmd_id.append(le_id)
            self.cmd_is_ext.append(cb_frame)
            self.cmd_len.append(sp_len)
            self.cmd_data.append(le_data)
            self.btn_once.append(b_once)
            self.btn_periodic.append(b_per)

            grid.addWidget(QLabel(f"Cmd{i+1}"), i, 0)
            grid.addWidget(QLabel("ID"), i, 1)
            grid.addWidget(le_id, i, 2)
            grid.addWidget(QLabel("Frame"), i, 3)
            grid.addWidget(cb_frame, i, 4)
            grid.addWidget(QLabel("Len"), i, 5)
            grid.addWidget(sp_len, i, 6)
            grid.addWidget(QLabel("Data"), i, 7)
            grid.addWidget(le_data, i, 8)
            grid.addWidget(b_once, i, 9)
            grid.addWidget(b_per, i, 10)
        layout.addWidget(g_cmd)

    def _build_page2(self):
        layout = QHBoxLayout(self.page2)
        left = QVBoxLayout()
        g_sel = QGroupBox("Signals（34 個 + 全部勾選）")
        gl = QGridLayout(g_sel)
        self.chk_all = QCheckBox("全部勾選")
        gl.addWidget(self.chk_all, 0, 0, 1, 2)
        self.chk_map = {}
        for idx, name in enumerate(SIGNALS_ALL):
            cb = QCheckBox(name)
            self.chk_map[name] = cb
            r = (idx // 2) + 1
            c = idx % 2
            gl.addWidget(cb, r, c)
        left.addWidget(g_sel)

        g_val = QGroupBox("Latest Values")
        v2 = QVBoxLayout(g_val)
        self.tbl_values = QTableWidget(0, 2)
        self.tbl_values.setHorizontalHeaderLabels(["Signal", "Value"])
        self.tbl_values.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tbl_values.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        v2.addWidget(self.tbl_values)
        row = QHBoxLayout()
        self.btn_export = QPushButton("匯出 CSV")
        self.btn_clear_data = QPushButton("clear_data")
        row.addWidget(self.btn_export)
        row.addWidget(self.btn_clear_data)
        v2.addLayout(row)
        left.addWidget(g_val, stretch=1)

        left_wrap = QWidget()
        left_wrap.setLayout(left)
        left_wrap.setMaximumWidth(560)

        right = QVBoxLayout()
        g_chart = QGroupBox("Line Chart（X軸 0 ~ 累積最大秒數）")
        vch = QVBoxLayout(g_chart)
        self.chart = QChart()
        self.chart.legend().setVisible(True)
        self.chart.setTitle("Signals")
        self.axis_x = QValueAxis()
        self.axis_x.setTitleText("Time (s)")
        self.axis_y = QValueAxis()
        self.axis_y.setTitleText("Value")
        self.chart.addAxis(self.axis_x, Qt.AlignBottom)
        self.chart.addAxis(self.axis_y, Qt.AlignLeft)
        self.axis_x.setRange(0, 1)
        self.axis_y.setRange(0, 100)
        self.chart_view = QChartView(self.chart)
        self.chart_view.setRenderHint(QPainter.Antialiasing)
        vch.addWidget(self.chart_view)
        right.addWidget(g_chart)
        right_wrap = QWidget()
        right_wrap.setLayout(right)
        layout.addWidget(left_wrap)
        layout.addWidget(right_wrap, stretch=1)

    def _build_page3(self):
        layout = QVBoxLayout(self.page3)
        splitter = QSplitter(Qt.Vertical)

        top_wrap = QWidget()
        top_layout = QVBoxLayout(top_wrap)
        g_tbl = QGroupBox("EKF 資訊表格（每輪一列）")
        vtbl = QVBoxLayout(g_tbl)

        btn_row = QHBoxLayout()
        self.btn_page3_start = QPushButton("開始")
        self.btn_page3_record_csv = QPushButton("記錄CSV")
        self.btn_page3_record_csv.setCheckable(True)
        self.btn_page3_stop = QPushButton("停止")
        self.btn_page3_clear = QPushButton("清除")
        self.btn_export_page3_csv = QPushButton("匯出 Page3 CSV")
        btn_row.addWidget(self.btn_page3_start)
        btn_row.addWidget(self.btn_page3_record_csv)
        btn_row.addWidget(self.btn_page3_stop)
        btn_row.addWidget(self.btn_page3_clear)
        btn_row.addWidget(self.btn_export_page3_csv)
        btn_row.addStretch(1)
        vtbl.addLayout(btn_row)

        self.tbl_page3 = QTableWidget(0, len(PAGE3_HEADERS))
        self.tbl_page3.setHorizontalHeaderLabels(PAGE3_HEADERS)
        self.tbl_page3.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_page3.setSelectionMode(QTableWidget.SingleSelection)
        self.tbl_page3.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_page3.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        for c in range(1, len(PAGE3_HEADERS)):
            self.tbl_page3.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.tbl_page3.verticalHeader().setVisible(False)
        vtbl.addWidget(self.tbl_page3)

        self.lbl_page3_table_tip = QLabel("第三頁時序：送一筆 TX -> 等對應 RX -> 再等 10ms -> 下一筆。12 筆完成後才結束本輪。")
        vtbl.addWidget(self.lbl_page3_table_tip)
        top_layout.addWidget(g_tbl)
        splitter.addWidget(top_wrap)

        bottom_wrap = QWidget()
        bottom_layout = QVBoxLayout(bottom_wrap)
        g_chart = QGroupBox("EKF Prediction / Measurement / Posterior 分布（停止後可回朔觀看）")
        vch = QVBoxLayout(g_chart)

        slider_row = QHBoxLayout()
        self.page3_slider = QSlider(Qt.Horizontal)
        self.page3_slider.setMinimum(0)
        self.page3_slider.setMaximum(0)
        self.page3_slider.setValue(0)
        self.lbl_page3_slider = QLabel("row: -")
        slider_row.addWidget(QLabel("回朔"))
        slider_row.addWidget(self.page3_slider)
        slider_row.addWidget(self.lbl_page3_slider)
        vch.addLayout(slider_row)

        self.ekf_chart = QChart()
        self.ekf_chart.legend().setVisible(True)
        self.ekf_chart.setTitle("Gaussian Pulling Visualization")
        self.ekf_axis_x = QValueAxis()
        self.ekf_axis_x.setTitleText("State")
        self.ekf_axis_y = QValueAxis()
        self.ekf_axis_y.setTitleText("Density")
        self.ekf_chart.addAxis(self.ekf_axis_x, Qt.AlignBottom)
        self.ekf_chart.addAxis(self.ekf_axis_y, Qt.AlignLeft)
        self.ekf_axis_x.setRange(0, 1)
        self.ekf_axis_y.setRange(0, 5)

        self.pred_series = QLineSeries()
        self.pred_series.setName("Prediction")
        self.meas_series = QLineSeries()
        self.meas_series.setName("Measurement")
        self.post_series = QLineSeries()
        self.post_series.setName("Posterior")
        for s in [self.pred_series, self.meas_series, self.post_series]:
            self.ekf_chart.addSeries(s)
            s.attachAxis(self.ekf_axis_x)
            s.attachAxis(self.ekf_axis_y)

        self.ekf_chart_view = QChartView(self.ekf_chart)
        self.ekf_chart_view.setRenderHint(QPainter.Antialiasing)
        vch.addWidget(self.ekf_chart_view)

        bottom_layout.addWidget(g_chart)
        self.lbl_page3_info = QLabel("Prediction≈soc-k0*innovation；Measurement≈soc_true；Posterior≈soc；Variance≈(p00+q00, S, p00)")
        bottom_layout.addWidget(self.lbl_page3_info)

        splitter.addWidget(bottom_wrap)
        splitter.setSizes([380, 520])
        layout.addWidget(splitter)

    def _build_page4(self):
        layout = QVBoxLayout(self.page4)
        top = QHBoxLayout()
        self.btn_page4_use_page3 = QPushButton("使用目前 Page3 資料")
        self.btn_page4_load_csv = QPushButton("載入 Page3 CSV")
        self.btn_page4_analyze = QPushButton("開始分析")
        self.btn_page4_clear = QPushButton("清除")
        self.btn_page4_export = QPushButton("匯出分析結果")
        top.addWidget(self.btn_page4_use_page3)
        top.addWidget(self.btn_page4_load_csv)
        top.addWidget(self.btn_page4_analyze)
        top.addWidget(self.btn_page4_clear)
        top.addWidget(self.btn_page4_export)
        top.addStretch(1)
        layout.addLayout(top)

        self.lbl_page4_source = QLabel("資料來源：未選擇")
        layout.addWidget(self.lbl_page4_source)

        splitter = QSplitter(Qt.Vertical)

        top_wrap = QWidget()
        top_layout = QVBoxLayout(top_wrap)
        g_summary = QGroupBox("Summary Table")
        vs = QVBoxLayout(g_summary)
        self.tbl_page4_summary = QTableWidget(0, 2)
        self.tbl_page4_summary.setHorizontalHeaderLabels(["Metric", "Value"])
        self.tbl_page4_summary.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tbl_page4_summary.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        vs.addWidget(self.tbl_page4_summary)
        top_layout.addWidget(g_summary)
        splitter.addWidget(top_wrap)

        bottom_wrap = QWidget()
        bottom_layout = QVBoxLayout(bottom_wrap)
        g_analysis = QGroupBox("分析結果 / 調整建議")
        va = QVBoxLayout(g_analysis)
        self.txt_page4_analysis = QPlainTextEdit()
        self.txt_page4_analysis.setReadOnly(True)
        va.addWidget(self.txt_page4_analysis)

        self.tbl_page4_advice = QTableWidget(0, 5)
        self.tbl_page4_advice.setHorizontalHeaderLabels(["現象", "可能原因", "建議調整參數", "調整方向", "風險提醒"])
        for c in range(5):
            self.tbl_page4_advice.horizontalHeader().setSectionResizeMode(c, QHeaderView.Stretch)
        va.addWidget(self.tbl_page4_advice)

        bottom_layout.addWidget(g_analysis)
        splitter.addWidget(bottom_wrap)
        splitter.setSizes([260, 520])
        layout.addWidget(splitter)

    def _wire(self):
        self.btn_connect.clicked.connect(self.toggle_connect)
        self.btn_refresh_channels.clicked.connect(self.refresh_pcan_channels)
        self.btn_clear.clicked.connect(self.txt_raw.clear)
        self.btn_apply_filter.clicked.connect(self.apply_id_filter)
        self.btn_clear_filter.clicked.connect(self.clear_id_filter)

        for i in range(3):
            self.btn_once[i].clicked.connect(lambda _=False, k=i: self.send_cmd_once(k))
            self.btn_periodic[i].toggled.connect(lambda checked, k=i: self.toggle_cmd_periodic(k, checked))
            self.cmd_periodic[i].timeout.connect(lambda k=i: self.send_cmd_once(k))

        self.chk_all.toggled.connect(self.on_select_all_toggled)
        for cb in self.chk_map.values():
            cb.toggled.connect(self.on_page2_selection_changed)

        self.page2_periodic.timeout.connect(self.page2_send_burst)

        self.btn_export.clicked.connect(self.export_csv)
        self.btn_clear_data.clicked.connect(self.clear_data)
        self.tabs.currentChanged.connect(self.on_tab_changed)

        self.btn_page3_start.clicked.connect(self.page3_controller.start)
        self.btn_page3_stop.clicked.connect(self.page3_controller.stop)
        self.btn_page3_clear.clicked.connect(self.clear_page3_data)
        self.btn_page3_record_csv.toggled.connect(self.page3_controller.set_record_csv)
        self.btn_export_page3_csv.clicked.connect(self.export_page3_csv)
        self.tbl_page3.itemSelectionChanged.connect(self.on_page3_row_selected)
        self.page3_slider.valueChanged.connect(self.on_page3_slider_changed)

        self.page3_controller.tx_frame.connect(self.on_page3_tx_frame)
        self.page3_controller.row_started.connect(self.on_page3_row_started)
        self.page3_controller.row_updated.connect(self.on_page3_row_updated)
        self.page3_controller.row_finished.connect(self.on_page3_row_finished)
        self.page3_controller.distribution_updated.connect(self.on_page3_distribution_updated)
        self.page3_controller.log_message.connect(lambda s: self._raw_text(f"[PAGE3] {s}"))

        self.btn_page4_use_page3.clicked.connect(self.page4_use_page3_data)
        self.btn_page4_load_csv.clicked.connect(self.page4_load_csv)
        self.btn_page4_analyze.clicked.connect(self.page4_analyze)
        self.btn_page4_clear.clicked.connect(self.page4_clear)
        self.btn_page4_export.clicked.connect(self.page4_export)

    def refresh_pcan_channels(self):
        current_val = self.cmb_channel.currentData() if hasattr(self, "cmb_channel") else None
        self.cmb_channel.blockSignals(True)
        self.cmb_channel.clear()

        try:
            channels = detect_available_pcan_channels(self.pcan if hasattr(self, "pcan") else None)
        except Exception:
            channels = build_usb_channels()

        if channels:
            for name, val in channels:
                self.cmb_channel.addItem(name, val)
        else:
            self.cmb_channel.addItem("No PCAN USB detected", None)

        if current_val is not None:
            try:
                old_val = to_int(current_val)
                for i in range(self.cmb_channel.count()):
                    if self.cmb_channel.itemData(i) is not None and to_int(self.cmb_channel.itemData(i)) == old_val:
                        self.cmb_channel.setCurrentIndex(i)
                        break
            except Exception:
                pass

        self.cmb_channel.blockSignals(False)

        if hasattr(self, "_raw_text"):
            self._raw_text(f"PCAN channels refreshed: {self.cmb_channel.count()} found")

    def _now(self):
        return QDateTime.currentDateTime().toString("HH:mm:ss.zzz")

    def _raw_enqueue(self, direction, can_id, dlc, data, is_ext, note=""):
        data_hex = " ".join(f"{b:02X}" for b in data[:dlc])
        line = f"[{self._now()}] {direction} ID=0x{can_id:08X} DLC={dlc} DATA={data_hex}"
        if note:
            line += f"  {note}"
        self._raw_queue.append((line, is_ext))

    def _raw_text(self, text):
        self._raw_queue.append((f"[{self._now()}] {text}", False))

    def _is_ext_combo(self, combo):
        return combo.currentText().upper() == "EXT"

    def _parse_hex_bytes(self, s):
        s = s.strip().replace(",", " ")
        if not s:
            return b""
        return bytes(int(p, 16) & 0xFF for p in s.split() if p)

    def _selected_signals(self):
        return [name for name, cb in self.chk_map.items() if cb.isChecked()]

    def _selected_signals_set(self):
        return set(self._selected_signals())

    @Slot(int, bytes, bool)
    def on_page3_tx_frame(self, can_id, payload, is_ext):
        now = time.perf_counter()
        note = ""
        if self._page3_last_tx_perf is not None:
            note = f"[Δt={(now - self._page3_last_tx_perf)*1000.0:.1f}ms]"
        self._page3_last_tx_perf = now

        self._send_can(can_id, payload, is_ext, log_tx=False)
        if self._id_filter_set is None or can_id in self._id_filter_set:
            self._raw_enqueue("TX", can_id, len(payload), payload, is_ext=is_ext, note=note)

    @Slot(int, str)
    def on_page3_row_started(self, round_idx, time_label):
        row = self.tbl_page3.rowCount()
        self.tbl_page3.insertRow(row)
        self.tbl_page3.setItem(row, 0, QTableWidgetItem(time_label))
        for c in range(1, len(PAGE3_HEADERS)):
            self.tbl_page3.setItem(row, c, QTableWidgetItem(""))
        self.page3_slider.setMaximum(max(0, self.tbl_page3.rowCount() - 1))

    @Slot(int, dict)
    def on_page3_row_updated(self, round_idx, values):
        if not (0 <= round_idx < self.tbl_page3.rowCount()):
            return
        for c, name in enumerate(PAGE3_HEADERS[1:], start=1):
            v = values.get(name, "")
            txt = f"{v:.6f}" if isinstance(v, float) else ""
            item = self.tbl_page3.item(round_idx, c)
            if item is None:
                item = QTableWidgetItem(txt)
                self.tbl_page3.setItem(round_idx, c, item)
            else:
                item.setText(txt)

    @Slot(int, dict)
    def on_page3_row_finished(self, round_idx, values):
        self.on_page3_row_updated(round_idx, values)

    @Slot(dict)
    def on_page3_distribution_updated(self, values):
        self.page3_last_values = dict(values)
        if self.page3_controller.state != Page3State.IDLE:
            self.refresh_page3_chart()

    def on_select_all_toggled(self, checked):
        if self._bulk_checking:
            return
        self._bulk_checking = True
        try:
            for cb in self.chk_map.values():
                cb.setChecked(checked)
        finally:
            self._bulk_checking = False

    def on_tab_changed(self, idx):
        pass

    def apply_id_filter(self):
        text = self.le_id_filter.text().strip()
        if not text:
            self._id_filter_set = None
            self._raw_text("ID filter cleared (empty)")
            return
        try:
            ids = set()
            for p in text.replace(",", " ").split():
                ids.add(int(p, 16))
            self._id_filter_set = ids
            pretty = ", ".join(f"0x{i:X}" for i in sorted(ids))
            self._raw_text(f"ID filter set: {pretty}")
        except Exception as e:
            QMessageBox.warning(self, "ID Filter", f"格式錯誤：{e}\n例: 753 或 0x753 或 0x752,0x753")

    def clear_id_filter(self):
        self._id_filter_set = None
        self.le_id_filter.setText("")
        self._raw_text("ID filter cleared")

    @Slot()
    def toggle_connect(self):
        if not self.connected:
            self.do_connect()
        else:
            self.do_disconnect()

    def do_connect(self):
        try:
            channel_data = self.cmb_channel.currentData()
            if channel_data is None:
                QMessageBox.information(self, "PCAN", "目前沒有偵測到可用的 PCAN USB，請確認裝置已連接後按『重新偵測』。")
                return
            self.channel_val = to_int(channel_data)
            baud_val = to_int(self.cmb_baud.currentData())
            st = to_int(self.pcan.Initialize(self.channel_val, baud_val))
            if st != PCAN_ERROR_OK:
                QMessageBox.warning(self, "PCAN", f"Initialize 失敗，錯誤碼: 0x{st:X}")
                return
            self.connected = True
            self.btn_connect.setText("斷線")
            self.lbl_status.setText("Status: Connected")
            self._raw_text("=== Connected ===")
            self.rx_thread = PcanRxThread(self.pcan, self.channel_val)
            self.rx_thread.frame_received.connect(self.on_frame)
            self.rx_thread.error_happened.connect(lambda s: self._raw_text(s))
            self.rx_thread.start_rx()
            self.on_page2_selection_changed()
        except Exception as e:
            QMessageBox.critical(self, "PCAN", f"連線失敗：{e}")

    def do_disconnect(self):
        for i in range(3):
            self.btn_periodic[i].setChecked(False)
            self.cmd_periodic[i].stop()
        self.page2_periodic.stop()
        self.page3_controller.stop()
        self._burst_queue.clear()
        self._burst_timer.stop()
        if self.rx_thread:
            self.rx_thread.stop_rx()
            self.rx_thread = None
        try:
            self.pcan.Uninitialize(self.channel_val)
        except Exception:
            pass
        self.connected = False
        self.btn_connect.setText("連線")
        self.lbl_status.setText("Status: Disconnected")
        self._raw_text("=== Disconnected ===")

    @Slot(object)
    def on_frame(self, frame):
        self._frame_queue.append(frame)
        is_ext = (to_int(frame.msgtype) & PCAN_MESSAGE_EXTENDED) != 0
        if self._id_filter_set is None or frame.can_id in self._id_filter_set:
            self._raw_enqueue("RX", frame.can_id, frame.dlc, frame.data, is_ext=is_ext)

    def flush_ui(self):
        self._decode_frames(limit=3000)
        if self.btn_pause.isChecked():
            return

        show_std = self.chk_show_std.isChecked()
        show_ext = self.chk_show_ext.isChecked()
        n = 0
        while self._raw_queue and n < 700:
            line, is_ext = self._raw_queue.popleft()
            if "===" in line or "ID filter" in line or "RX 讀取錯誤" in line or "[PAGE3]" in line or "Page2" in line:
                self.txt_raw.appendPlainText(line)
                n += 1
                continue
            if is_ext and not show_ext:
                continue
            if (not is_ext) and not show_std:
                continue
            self.txt_raw.appendPlainText(line)
            n += 1

    def _decode_frames(self, limit=3000):
        if not self._frame_queue:
            return
        selected = self._selected_signals_set()
        for _ in range(min(limit, len(self._frame_queue))):
            self._try_decode(self._frame_queue.popleft(), selected)

    def _send_can(self, can_id, data, is_ext, log_tx=True):
        if not self.connected:
            return False
        can_id_i = to_int(can_id)
        dlc = min(len(data), 8)
        payload = (data + b"\x00" * 8)[:dlc]
        if log_tx and (self._id_filter_set is None or can_id_i in self._id_filter_set):
            self._raw_enqueue("TX", can_id_i, dlc, payload, is_ext=is_ext)

        msg = TPCANMsg()
        msg.ID = can_id_i
        msg.LEN = to_int(dlc)
        msg.MSGTYPE = to_int(PCAN_MESSAGE_EXTENDED if is_ext else PCAN_MESSAGE_STANDARD)
        for i in range(8):
            msg.DATA[i] = 0
        for i in range(dlc):
            msg.DATA[i] = int(payload[i]) & 0xFF

        st = to_int(self.pcan.Write(self.channel_val, msg))
        if st != PCAN_ERROR_OK:
            if log_tx and (self._id_filter_set is None or can_id_i in self._id_filter_set):
                self._raw_enqueue("TX", can_id_i, dlc, payload, is_ext=is_ext, note=f"[ERROR 0x{st:X}]")
            if (st & PCAN_ERROR_BUSOFF) != 0:
                self._raw_text("BUS-OFF -> stop all periodic TX")
                for i in range(3):
                    self.btn_periodic[i].setChecked(False)
                    self.cmd_periodic[i].stop()
                self.page2_periodic.stop()
                self.page3_controller.stop()
                self._burst_queue.clear()
                self._burst_timer.stop()
            return False
        return True

    def send_cmd_once(self, idx):
        try:
            can_id = int(self.cmd_id[idx].text().strip(), 16)
            dlc = int(self.cmd_len[idx].value())
            data = self._parse_hex_bytes(self.cmd_data[idx].text())
            data = (data + b"\x00" * 8)[:dlc]
            is_ext = self._is_ext_combo(self.cmd_is_ext[idx])
            self._send_can(can_id, data, is_ext, True)
        except Exception as e:
            self._raw_text(f"Cmd{idx+1} 送出失敗：{e}")

    def toggle_cmd_periodic(self, idx, checked):
        if checked:
            self.cmd_periodic[idx].start()
            self.btn_periodic[idx].setText("週期中(1s)")
        else:
            self.cmd_periodic[idx].stop()
            self.btn_periodic[idx].setText("1s 週期送出")

    def on_page2_selection_changed(self):
        if self._bulk_checking:
            return
        selected = self._selected_signals_set()
        self._bulk_checking = True
        try:
            with QSignalBlocker(self.chk_all):
                self.chk_all.setChecked(len(selected) == len(self.chk_map))
        finally:
            self._bulk_checking = False
        if self.connected and selected:
            if not self.page2_periodic.isActive():
                self.page2_periodic.start()
                self._raw_text("Page2 periodic ON (1s)")
        else:
            if self.page2_periodic.isActive():
                self.page2_periodic.stop()
                self._raw_text("Page2 periodic OFF")
        self._sync_series()
        self._refresh_value_table()

    def page2_send_burst(self):
        if not self.connected:
            return
        selected = self._selected_signals_set()
        if not selected:
            return
        items = deque()
        if "rsoc" in selected:
            items.append(("rsoc", RSOC_TX))
        for name, code in EKF_SOC_CODES:
            if name in selected:
                items.append((name, bytes([code, 0x02])))
        if "r0" in selected:
            items.append(("r0", R0_TX))
        for name, code in EKF_R0_CODES:
            if name in selected:
                items.append((name, bytes([code, 0x02])))
        self._append_burst_items(items)

    def _append_burst_items(self, items):
        if not items:
            return
        self._burst_queue.extend(items)
        if not self._burst_timer.isActive():
            self._burst_timer.start()

    def _burst_send_next(self):
        if not self._burst_queue:
            self._burst_timer.stop()
            return
        _name, payload = self._burst_queue.popleft()
        self._send_can(TX_REQ_ID, payload, True, True)

    def _try_decode(self, frame, selected):
        if frame.can_id != RX_RSP_ID:
            return
        d = (frame.data + b"\x00" * 8)[:8]
        dlc = frame.dlc

        if self._waiting_first_sample_after_clear:
            self._t0 = time.monotonic()
            self._waiting_first_sample_after_clear = False

        t = time.monotonic() - self._t0
        if t > self._max_t_seen:
            self._max_t_seen = t

        updated_any = False

        if "rsoc" in selected and dlc >= 6 and d[0] == 0x05 and d[1] == 0x00:
            rsoc_val = struct.unpack("<f", bytes([d[2], d[3], d[4], d[5]]))[0]
            self._push_value("rsoc", t, float(rsoc_val))
            updated_any = True

        if dlc >= 4 and d[1] == 0x02:
            code = d[0]
            if code in CODE_TO_SOC:
                name = CODE_TO_SOC[code]
                if name in selected:
                    self._push_value(name, t, u16_le(d[2], d[3]) / 10.0)
                    updated_any = True
                r0_name = CODE_TO_R0.get(code)
                if r0_name in selected and dlc >= 6:
                    self._push_value(r0_name, t, float(u16_le(d[4], d[5])))
                    updated_any = True

            if code == 0x41 and "r0" in selected:
                self._push_value("r0", t, float(u16_le(d[2], d[3])))
                updated_any = True

            if code in PAGE3_FIELD_BY_CODE:
                self.page3_controller.on_rx_frame(frame.can_id, d)

        if updated_any:
            row = {"timestamp": QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss.zzz")}
            for s in selected:
                row[s] = self.latest_value.get(s, "")
            self.csv_rows.append(row)

    def _push_value(self, name, t, val):
        self.latest_value[name] = val
        self.data_buf[name].append((t, val))

    def _sync_series(self):
        selected = self._selected_signals_set()
        for name in list(self.series_map.keys()):
            if name not in selected:
                self.chart.removeSeries(self.series_map[name])
                del self.series_map[name]
        for name in selected:
            if name not in self.series_map:
                s = QLineSeries()
                s.setName(name)
                self.chart.addSeries(s)
                s.attachAxis(self.axis_x)
                s.attachAxis(self.axis_y)
                self.series_map[name] = s

    def _refresh_value_table(self):
        selected = self._selected_signals()
        self.tbl_values.setRowCount(len(selected))
        for r, name in enumerate(selected):
            self.tbl_values.setItem(r, 0, QTableWidgetItem(name))
            v = self.latest_value.get(name, "")
            if isinstance(v, float):
                self.tbl_values.setItem(r, 1, QTableWidgetItem(f"{v:.6f}" if name == "rsoc" else f"{v:.2f}"))
            else:
                self.tbl_values.setItem(r, 1, QTableWidgetItem(str(v)))

    def refresh_chart(self):
        selected = self._selected_signals_set()
        if not selected:
            return
        x_max = max(1.0, self._max_t_seen)
        self.axis_x.setRange(0.0, x_max)
        y_min = None
        y_max = None
        for name in selected:
            series = self.series_map.get(name)
            buf = self.data_buf.get(name)
            if not series or not buf:
                continue
            pts = [QPointF(t, float(v)) for (t, v) in buf]
            series.replace(pts)
            for _, v in buf:
                y_min = v if y_min is None else min(y_min, v)
                y_max = v if y_max is None else max(y_max, v)
        if y_min is None or y_max is None:
            self.axis_y.setRange(0, 100)
        else:
            margin = max(0.1, (y_max - y_min) * 0.05)
            self.axis_y.setRange(y_min - margin, y_max + margin)
        self._refresh_value_table()

    def on_page3_row_selected(self):
        rows = self.tbl_page3.selectionModel().selectedRows()
        if not rows:
            return
        r = rows[0].row()
        self.page3_selected_index = r
        with QSignalBlocker(self.page3_slider):
            self.page3_slider.setValue(r)
        self.lbl_page3_slider.setText(f"row: {r}")
        self.refresh_page3_chart()

    def on_page3_slider_changed(self, value):
        if 0 <= value < self.tbl_page3.rowCount():
            self.page3_selected_index = value
            self.lbl_page3_slider.setText(f"row: {value}")
            with QSignalBlocker(self.tbl_page3):
                self.tbl_page3.selectRow(value)
            self.refresh_page3_chart()

    def _gaussian_points(self, mu, sigma2, xmin, xmax, n=200):
        sigma2 = max(float(sigma2), 1e-9)
        sigma = math.sqrt(sigma2)
        pts = []
        step = (xmax - xmin) / max(n - 1, 1)
        norm = 1.0 / (sigma * math.sqrt(2.0 * math.pi))
        for i in range(n):
            x = xmin + i * step
            z = (x - mu) / sigma
            y = norm * math.exp(-0.5 * z * z)
            pts.append(QPointF(x, y))
        return pts

    def refresh_page3_chart(self):
        src = None
        row = self.page3_controller.get_row(self.page3_selected_index)
        if row is not None:
            src = row.values
            label_time = row.time_label
        elif self.page3_last_values:
            src = self.page3_last_values
            label_time = "latest"
        else:
            return

        soc = float(src.get("soc", 0.5))
        soc_true = float(src.get("soc_true", soc))
        p00 = max(abs(float(src.get("p00", 0.01))), 1e-9)
        q00 = max(abs(float(src.get("q00", 0.001))), 1e-9)
        S = max(abs(float(src.get("S", 0.01))), 1e-9)
        innovation = float(src.get("innovation", 0.0))
        k0 = float(src.get("k0", 0.0))
        k1 = float(src.get("k1", 0.0))

        pred_mu = soc - k0 * innovation
        meas_mu = soc_true
        post_mu = soc

        pred_var = max(p00 + q00, 1e-9)
        meas_var = max(S, 1e-9)
        post_var = max(p00, 1e-9)

        left = min(pred_mu - 4 * math.sqrt(pred_var), meas_mu - 4 * math.sqrt(meas_var), post_mu - 4 * math.sqrt(post_var))
        right = max(pred_mu + 4 * math.sqrt(pred_var), meas_mu + 4 * math.sqrt(meas_var), post_mu + 4 * math.sqrt(post_var))
        if left == right:
            left -= 1.0
            right += 1.0

        pred_pts = self._gaussian_points(pred_mu, pred_var, left, right)
        meas_pts = self._gaussian_points(meas_mu, meas_var, left, right)
        post_pts = self._gaussian_points(post_mu, post_var, left, right)

        self.pred_series.replace(pred_pts)
        self.meas_series.replace(meas_pts)
        self.post_series.replace(post_pts)

        ymax = max([p.y() for p in pred_pts + meas_pts + post_pts] + [1.0])
        self.ekf_axis_x.setRange(left, right)
        self.ekf_axis_y.setRange(0.0, ymax * 1.1)

        direction = "→" if innovation >= 0 else "←"
        self.lbl_page3_info.setText(
            f"時間 {label_time} | innovation={innovation:.6f} {direction} | "
            f"k0={k0:.6f}, k1={k1:.6f} | "
            f"Prediction μ={pred_mu:.6f}, Measurement μ={meas_mu:.6f}, Posterior μ={post_mu:.6f}"
        )

    def clear_page3_data(self):
        self.page3_controller.clear()
        self.page3_last_values.clear()
        self.page3_selected_index = -1
        self._page3_last_tx_perf = None

        self.tbl_page3.setRowCount(0)
        with QSignalBlocker(self.page3_slider):
            self.page3_slider.setMinimum(0)
            self.page3_slider.setMaximum(0)
            self.page3_slider.setValue(0)
        self.lbl_page3_slider.setText("row: -")
        self.pred_series.clear()
        self.meas_series.clear()
        self.post_series.clear()
        self.ekf_axis_x.setRange(0.0, 1.0)
        self.ekf_axis_y.setRange(0.0, 1.0)
        self._raw_text("[PAGE3] clear")

    def page4_use_page3_data(self):
        self.page4_data_rows = self.page3_controller.export_rows()
        self.page4_source_name = f"Page3 current rows: {len(self.page4_data_rows)}"
        self.lbl_page4_source.setText(f"資料來源：{self.page4_source_name}")

    def page4_load_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "載入 Page3 CSV", "", "CSV Files (*.csv)")
        if not path:
            return
        rows = []
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    rows.append(row)
            self.page4_data_rows = rows
            self.page4_source_name = os.path.basename(path)
            self.lbl_page4_source.setText(f"資料來源：{self.page4_source_name} ({len(rows)} rows)")
        except Exception as e:
            QMessageBox.warning(self, "Page4", f"載入 CSV 失敗：{e}")

    def _page4_num(self, row, key):
        try:
            v = row.get(key, "")
            if v == "" or v is None:
                return None
            return float(v)
        except Exception:
            return None

    def page4_analyze(self):
        rows = self.page4_data_rows
        if not rows:
            QMessageBox.information(self, "Page4", "目前沒有可分析資料。")
            return

        innovations = []
        abs_innov = []
        Ss = []
        k0s = []
        k1s = []
        p00s = []
        p11s = []
        q00s = []
        q11s = []
        Rs = []
        errs = []

        for r in rows:
            innovation = self._page4_num(r, "innovation")
            S = self._page4_num(r, "S")
            k0 = self._page4_num(r, "k0")
            k1 = self._page4_num(r, "k1")
            p00 = self._page4_num(r, "p00")
            p11 = self._page4_num(r, "p11")
            q00 = self._page4_num(r, "q00")
            q11 = self._page4_num(r, "q11")
            Rv = self._page4_num(r, "R")
            soc = self._page4_num(r, "soc")
            soc_true = self._page4_num(r, "soc_true")

            if innovation is not None:
                innovations.append(innovation)
                abs_innov.append(abs(innovation))
            if S is not None:
                Ss.append(S)
            if k0 is not None:
                k0s.append(k0)
            if k1 is not None:
                k1s.append(k1)
            if p00 is not None:
                p00s.append(p00)
            if p11 is not None:
                p11s.append(p11)
            if q00 is not None:
                q00s.append(q00)
            if q11 is not None:
                q11s.append(q11)
            if Rv is not None:
                Rs.append(Rv)
            if soc is not None and soc_true is not None:
                errs.append(soc - soc_true)

        def mean(xs):
            return sum(xs) / len(xs) if xs else None

        def std(xs):
            if not xs:
                return None
            m = mean(xs)
            return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))

        def minmax(xs):
            return (min(xs), max(xs)) if xs else (None, None)

        summary = [
            ("rows", len(rows)),
            ("innovation_mean", mean(innovations)),
            ("innovation_abs_mean", mean(abs_innov)),
            ("innovation_std", std(innovations)),
            ("S_mean", mean(Ss)),
            ("k0_mean", mean(k0s)),
            ("k0_min", minmax(k0s)[0]),
            ("k0_max", minmax(k0s)[1]),
            ("k1_mean", mean(k1s)),
            ("k1_min", minmax(k1s)[0]),
            ("k1_max", minmax(k1s)[1]),
            ("p00_mean", mean(p00s)),
            ("p11_mean", mean(p11s)),
            ("q00_mean", mean(q00s)),
            ("q11_mean", mean(q11s)),
            ("R_mean", mean(Rs)),
            ("soc_err_mean", mean(errs)),
            ("soc_err_abs_mean", mean([abs(x) for x in errs]) if errs else None),
            ("soc_err_max_abs", max([abs(x) for x in errs]) if errs else None),
        ]

        self.tbl_page4_summary.setRowCount(len(summary))
        for r, (k, v) in enumerate(summary):
            self.tbl_page4_summary.setItem(r, 0, QTableWidgetItem(str(k)))
            txt = f"{v:.6f}" if isinstance(v, float) else str(v)
            self.tbl_page4_summary.setItem(r, 1, QTableWidgetItem(txt))

        advice = []
        analysis_lines = []

        innovation_abs_mean = mean(abs_innov) or 0.0
        innovation_std = std(innovations) or 0.0
        k0_mean = mean(k0s) or 0.0
        p00_mean = mean(p00s) or 0.0
        S_mean = mean(Ss) or 0.0
        err_abs_mean = mean([abs(x) for x in errs]) if errs else 0.0

        analysis_lines.append(f"資料列數：{len(rows)}")
        analysis_lines.append(f"innovation_abs_mean = {innovation_abs_mean:.6f}")
        analysis_lines.append(f"k0_mean = {k0_mean:.6f}")
        analysis_lines.append(f"p00_mean = {p00_mean:.6f}")
        analysis_lines.append(f"S_mean = {S_mean:.6f}")
        analysis_lines.append(f"soc_abs_error_mean = {err_abs_mean:.6f}")

        if innovation_abs_mean > 0.02 and k0_mean < 0.1:
            advice.append(("innovation 過大且 k0 太小", "模型跟不上但修正太弱", "q00 / R", "q00 ↑ 或 R ↓", "太大可能引入震盪"))
        if innovation_std > 0.02 and k0_mean > 0.5:
            advice.append(("估測震盪", "量測權重可能過高", "R / q00", "R ↑ 或 q00 ↓", "R 太大會變慢"))
        if err_abs_mean > 0.02 and innovation_abs_mean < 0.01:
            advice.append(("誤差仍存在但 innovation 偏小", "模型可能自我一致但偏了", "q00 / 模型", "q00 ↑，並檢查 OCV / 容量 / R0", "只調 Q/R 可能不夠"))
        if p00_mean < 1e-5 and err_abs_mean > 0.01:
            advice.append(("p00 太小但誤差還在", "濾波器過度自信", "q00 / p00 初值", "q00 ↑ 或 p00 初值 ↑", "可能暫時增加抖動"))
        if p00_mean > 0.1:
            advice.append(("p00 長期偏大", "系統一直不確定", "q00 / R", "q00 ↓ 或檢查 R", "可能收斂慢"))
        if S_mean < 1e-4 and innovation_abs_mean > 0.02:
            advice.append(("S 太小但 innovation 大", "低估量測不確定性", "R", "R ↑", "量測主導過強"))
        if S_mean > 0.05 and k0_mean < 0.05:
            advice.append(("S 太大且 k0 幾乎為 0", "幾乎不信量測", "R / q00", "R ↓ 或 q00 ↑", "可能噪聲敏感"))

        if not advice:
            advice.append(("目前無明顯異常", "統計上未見強烈調整訊號", "-", "先小步調整", "避免一次改太多"))

        self.txt_page4_analysis.setPlainText("\n".join(analysis_lines))
        self.tbl_page4_advice.setRowCount(len(advice))
        for r, row in enumerate(advice):
            for c, val in enumerate(row):
                self.tbl_page4_advice.setItem(r, c, QTableWidgetItem(str(val)))

    def page4_clear(self):
        self.page4_data_rows = []
        self.page4_source_name = ""
        self.lbl_page4_source.setText("資料來源：未選擇")
        self.tbl_page4_summary.setRowCount(0)
        self.tbl_page4_advice.setRowCount(0)
        self.txt_page4_analysis.clear()

    def page4_export(self):
        if self.tbl_page4_summary.rowCount() == 0 and self.tbl_page4_advice.rowCount() == 0:
            QMessageBox.information(self, "Page4", "目前沒有可匯出分析結果。")
            return
        path, _ = QFileDialog.getSaveFileName(self, "匯出分析結果", "page4_analysis.txt", "Text Files (*.txt)")
        if not path:
            return
        try:
            lines = [self.lbl_page4_source.text(), "", "[Summary]"]
            for r in range(self.tbl_page4_summary.rowCount()):
                k = self.tbl_page4_summary.item(r, 0).text()
                v = self.tbl_page4_summary.item(r, 1).text()
                lines.append(f"{k}: {v}")
            lines += ["", "[Analysis]", self.txt_page4_analysis.toPlainText(), "", "[Advice]"]
            for r in range(self.tbl_page4_advice.rowCount()):
                vals = [self.tbl_page4_advice.item(r, c).text() if self.tbl_page4_advice.item(r, c) else "" for c in range(5)]
                lines.append(" | ".join(vals))
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            QMessageBox.information(self, "Page4", "匯出完成。")
        except Exception as e:
            QMessageBox.warning(self, "Page4", f"匯出失敗：{e}")

    def export_csv(self):
        if not self.csv_rows:
            QMessageBox.information(self, "CSV", "目前沒有資料可匯出。")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save CSV", "pcan_signals.csv", "CSV Files (*.csv)")
        if not path:
            return
        headers = ["timestamp"] + SIGNALS_ALL
        writer = CsvAutoSplitWriter(path, headers, max_bytes=100 * 1024 * 1024)
        try:
            for row in self.csv_rows:
                writer.write_row(row)
            writer.close()
            QMessageBox.information(self, "CSV", "匯出完成。\n若超過100MB會自動分割為多個檔案。")
        except Exception as e:
            writer.close()
            QMessageBox.warning(self, "CSV", f"匯出失敗：{e}")

    def export_page3_csv(self):
        rows = self.page3_controller.export_rows()
        if not rows:
            QMessageBox.information(self, "Page3 CSV", "目前沒有 Page3 資料可匯出。")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save Page3 CSV", "page3_ekf_table.csv", "CSV Files (*.csv)")
        if not path:
            return
        writer = CsvAutoSplitWriter(path, PAGE3_HEADERS, max_bytes=100 * 1024 * 1024)
        try:
            for row in rows:
                writer.write_row(row)
            writer.close()
            QMessageBox.information(self, "Page3 CSV", "匯出完成。\n若超過100MB會自動分割。")
        except Exception as e:
            writer.close()
            QMessageBox.warning(self, "Page3 CSV", f"匯出失敗：{e}")

    def clear_data(self):
        self.csv_rows.clear()
        self.latest_value.clear()
        for name in self.data_buf:
            self.data_buf[name].clear()
        for series in self.series_map.values():
            series.clear()
        self._max_t_seen = 0.0
        self._waiting_first_sample_after_clear = True
        self.axis_x.setRange(0.0, 1.0)
        self.axis_y.setRange(0.0, 100.0)
        self._refresh_value_table()
        self.clear_page3_data()
        self.page4_clear()

    def closeEvent(self, event):
        try:
            if self.connected:
                self.do_disconnect()
        finally:
            super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
