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
        self.current_row = Page3Row(round_index=self.round_index, time_label=f"{self.round_index}.00")
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
        base = now if self.round_start_time is None else max(self.round_start_time + 1.0, now)
        self.next_round_time = base
        self.log_message.emit(f"Round {self.round_index} finished; next round after {max(0.0, base-now):.3f}s")
        self.round_index += 1
        self._set_state(Page3State.ROUND_DONE)

    def _set_state(self, state: Page3State) -> None:
        self.state = state
        self.state_changed.emit(state.name)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PCAN Qt6 UI - v11")
        self.resize(1680, 1040)
        self.pcan = PCANBasic()
        self.connected = False
        self.channel_val = 0
        self.rx_thread = None
        self._frame_queue = deque(maxlen=120000)
        self._raw_queue = deque(maxlen=120000)
        self._id_filter_set = None
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
        self._burst_queue = deque()
        self._burst_timer = QTimer(self)
        self._burst_timer.setInterval(10)
        self._burst_timer.timeout.connect(self._burst_send_next)
        self.max_points = 20000
        self.data_buf = {k: deque(maxlen=self.max_points) for k in SIGNALS_ALL}
        self.latest_value = {}
        self.csv_rows = []
        self.page3_controller = Page3Controller(self)
        self.page3_last_values = {}
        self.page3_selected_index = -1
        self._page3_last_tx_perf = None
        self.page4_data_rows = []
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
        self.page1 = QWidget(); self.page2 = QWidget(); self.page3 = QWidget(); self.page4 = QWidget()
        self.tabs.addTab(self.page1, "Page1")
        self.tabs.addTab(self.page2, "Page2")
        self.tabs.addTab(self.page3, "Page3")
        self.tabs.addTab(self.page4, "Page4")
        self._build_page1(); self._build_page2(); self._build_page3(); self._build_page4()
        act_export = QAction("匯出 CSV", self)
        act_export.triggered.connect(self.export_csv)
        self.menuBar().addMenu("File").addAction(act_export)

    def _build_page1(self):
        layout = QVBoxLayout(self.page1)
        g_conn = QGroupBox("連線參數"); gl = QGridLayout(g_conn)
        self.cmb_channel = QComboBox()
        self.btn_refresh_channels = QPushButton("重新偵測")
        self.refresh_pcan_channels()
        self.cmb_baud = QComboBox()
        for k, v in BAUD_MAP.items(): self.cmb_baud.addItem(k, v)
        self.cmb_baud.setCurrentText("250K")
        self.cmb_txframe = QComboBox(); self.cmb_txframe.addItems(["STD", "EXT"]); self.cmb_txframe.setCurrentText("EXT")
        self.btn_connect = QPushButton("連線")
        self.lbl_status = QLabel("Status: Disconnected")
        gl.addWidget(QLabel("Channel"),0,0); gl.addWidget(self.cmb_channel,0,1); gl.addWidget(self.btn_refresh_channels,0,2)
        gl.addWidget(QLabel("Baud"),0,3); gl.addWidget(self.cmb_baud,0,4); gl.addWidget(QLabel("Tx Frame"),0,5); gl.addWidget(self.cmb_txframe,0,6)
        gl.addWidget(self.btn_connect,0,7); gl.addWidget(self.lbl_status,0,8); layout.addWidget(g_conn)
        g_raw = QGroupBox("CAN RAW (TX/RX) + Filters"); vraw = QVBoxLayout(g_raw); ctrl = QHBoxLayout()
        self.chk_show_std = QCheckBox("STD"); self.chk_show_ext = QCheckBox("EXT"); self.chk_show_ext.setChecked(True)
        self.le_id_filter = QLineEdit(""); self.le_id_filter.setPlaceholderText("ID filter(hex), e.g. 753 or 0x753 or 0x752,0x753")
        self.btn_apply_filter = QPushButton("Apply"); self.btn_clear_filter = QPushButton("Clear Filter")
        self.btn_pause = QPushButton("Pause"); self.btn_pause.setCheckable(True); self.btn_clear = QPushButton("Clear")
        for w in [QLabel("Show:"), self.chk_show_std, self.chk_show_ext, QLabel("ID Filter:"), self.le_id_filter, self.btn_apply_filter, self.btn_clear_filter, self.btn_pause, self.btn_clear]: ctrl.addWidget(w)
        ctrl.addStretch(1)
        self.txt_raw = QPlainTextEdit(); self.txt_raw.setReadOnly(True); self.txt_raw.setMaximumBlockCount(12000); self.txt_raw.setFont(QFont("Consolas",10))
        vraw.addLayout(ctrl); vraw.addWidget(self.txt_raw); layout.addWidget(g_raw, stretch=2)
        g_cmd = QGroupBox("Commands (3 groups)"); grid = QGridLayout(g_cmd)
        self.cmd_id=[]; self.cmd_len=[]; self.cmd_data=[]; self.cmd_is_ext=[]; self.btn_once=[]; self.btn_periodic=[]
        defaults=[("00000752","EXT",2,"70 00"),("00000752","EXT",2,"71 00"),("00000752","EXT",2,"72 00")]
        for i,(did,dfrm,dlen,ddat) in enumerate(defaults):
            le_id=QLineEdit(did); cb_frame=QComboBox(); cb_frame.addItems(["STD","EXT"]); cb_frame.setCurrentText(dfrm); sp_len=QSpinBox(); sp_len.setRange(0,8); sp_len.setValue(dlen); le_data=QLineEdit(ddat); b_once=QPushButton("單次送出"); b_per=QPushButton("1s 週期送出"); b_per.setCheckable(True)
            self.cmd_id.append(le_id); self.cmd_is_ext.append(cb_frame); self.cmd_len.append(sp_len); self.cmd_data.append(le_data); self.btn_once.append(b_once); self.btn_periodic.append(b_per)
            grid.addWidget(QLabel(f"Cmd{i+1}"),i,0); grid.addWidget(le_id,i,1); grid.addWidget(cb_frame,i,2); grid.addWidget(sp_len,i,3); grid.addWidget(le_data,i,4); grid.addWidget(b_once,i,5); grid.addWidget(b_per,i,6)
        layout.addWidget(g_cmd)

    def _build_page2(self):
        layout=QHBoxLayout(self.page2); left=QVBoxLayout(); g_sel=QGroupBox("Signals（34 個 + 全部勾選）"); gl=QGridLayout(g_sel)
        self.chk_all=QCheckBox("全部勾選"); gl.addWidget(self.chk_all,0,0,1,2); self.chk_map={}
        for idx,name in enumerate(SIGNALS_ALL):
            cb=QCheckBox(name); self.chk_map[name]=cb; gl.addWidget(cb,(idx//2)+1,idx%2)
        left.addWidget(g_sel); g_val=QGroupBox("Latest Values"); v2=QVBoxLayout(g_val); self.tbl_values=QTableWidget(0,2); self.tbl_values.setHorizontalHeaderLabels(["Signal","Value"]); v2.addWidget(self.tbl_values)
        row=QHBoxLayout(); self.btn_export=QPushButton("匯出 CSV"); self.btn_clear_data=QPushButton("clear_data"); row.addWidget(self.btn_export); row.addWidget(self.btn_clear_data); v2.addLayout(row); left.addWidget(g_val)
        lw=QWidget(); lw.setLayout(left); lw.setMaximumWidth(560)
        self.chart=QChart(); self.axis_x=QValueAxis(); self.axis_y=QValueAxis(); self.chart.addAxis(self.axis_x,Qt.AlignBottom); self.chart.addAxis(self.axis_y,Qt.AlignLeft); self.axis_x.setRange(0,1); self.axis_y.setRange(0,100); self.chart_view=QChartView(self.chart); rw=QWidget(); rv=QVBoxLayout(rw); rv.addWidget(self.chart_view); layout.addWidget(lw); layout.addWidget(rw,1)

    def _build_page3(self):
        layout=QVBoxLayout(self.page3); self.tbl_page3=QTableWidget(0,len(PAGE3_HEADERS)); self.tbl_page3.setHorizontalHeaderLabels(PAGE3_HEADERS)
        bar=QHBoxLayout(); self.btn_page3_start=QPushButton("開始"); self.btn_page3_record_csv=QPushButton("記錄CSV"); self.btn_page3_record_csv.setCheckable(True); self.btn_page3_stop=QPushButton("停止"); self.btn_page3_clear=QPushButton("清除"); self.btn_export_page3_csv=QPushButton("匯出 Page3 CSV")
        for w in [self.btn_page3_start,self.btn_page3_record_csv,self.btn_page3_stop,self.btn_page3_clear,self.btn_export_page3_csv]: bar.addWidget(w)
        layout.addLayout(bar); layout.addWidget(self.tbl_page3)
        self.page3_slider=QSlider(Qt.Horizontal); self.lbl_page3_slider=QLabel("row: -"); layout.addWidget(self.page3_slider); layout.addWidget(self.lbl_page3_slider)
        self.ekf_chart=QChart(); self.ekf_axis_x=QValueAxis(); self.ekf_axis_y=QValueAxis(); self.ekf_chart.addAxis(self.ekf_axis_x,Qt.AlignBottom); self.ekf_chart.addAxis(self.ekf_axis_y,Qt.AlignLeft)
        self.pred_series=QLineSeries(); self.pred_series.setName("Prediction"); self.meas_series=QLineSeries(); self.meas_series.setName("Measurement"); self.post_series=QLineSeries(); self.post_series.setName("Posterior")
        for s in [self.pred_series,self.meas_series,self.post_series]: self.ekf_chart.addSeries(s); s.attachAxis(self.ekf_axis_x); s.attachAxis(self.ekf_axis_y)
        self.ekf_chart_view=QChartView(self.ekf_chart); layout.addWidget(self.ekf_chart_view); self.lbl_page3_info=QLabel(""); layout.addWidget(self.lbl_page3_info)

    def _build_page4(self):
        layout=QVBoxLayout(self.page4); top=QHBoxLayout(); self.btn_page4_use_page3=QPushButton("使用目前 Page3 資料"); self.btn_page4_load_csv=QPushButton("載入 Page3 CSV"); self.btn_page4_analyze=QPushButton("開始分析"); self.btn_page4_clear=QPushButton("清除"); self.btn_page4_export=QPushButton("匯出分析結果")
        for w in [self.btn_page4_use_page3,self.btn_page4_load_csv,self.btn_page4_analyze,self.btn_page4_clear,self.btn_page4_export]: top.addWidget(w)
        layout.addLayout(top); self.lbl_page4_source=QLabel("資料來源：未選擇"); layout.addWidget(self.lbl_page4_source); self.tbl_page4_summary=QTableWidget(0,2); self.tbl_page4_summary.setHorizontalHeaderLabels(["Metric","Value"]); layout.addWidget(self.tbl_page4_summary); self.txt_page4_analysis=QPlainTextEdit(); self.txt_page4_analysis.setReadOnly(True); layout.addWidget(self.txt_page4_analysis); self.tbl_page4_advice=QTableWidget(0,5); self.tbl_page4_advice.setHorizontalHeaderLabels(["現象","可能原因","建議調整參數","調整方向","風險提醒"]); layout.addWidget(self.tbl_page4_advice)

    def _wire(self):
        self.btn_connect.clicked.connect(self.toggle_connect); self.btn_refresh_channels.clicked.connect(self.refresh_pcan_channels); self.btn_clear.clicked.connect(self.txt_raw.clear); self.btn_apply_filter.clicked.connect(self.apply_id_filter); self.btn_clear_filter.clicked.connect(self.clear_id_filter)
        for i in range(3): self.btn_once[i].clicked.connect(lambda _=False,k=i:self.send_cmd_once(k)); self.btn_periodic[i].toggled.connect(lambda checked,k=i:self.toggle_cmd_periodic(k,checked)); self.cmd_periodic[i].timeout.connect(lambda k=i:self.send_cmd_once(k))
        self.chk_all.toggled.connect(self.on_select_all_toggled)
        for cb in self.chk_map.values(): cb.toggled.connect(self.on_page2_selection_changed)
        self.page2_periodic.timeout.connect(self.page2_send_burst); self.btn_export.clicked.connect(self.export_csv); self.btn_clear_data.clicked.connect(self.clear_data)
        self.btn_page3_start.clicked.connect(self.page3_controller.start); self.btn_page3_stop.clicked.connect(self.page3_controller.stop); self.btn_page3_clear.clicked.connect(self.clear_page3_data); self.btn_page3_record_csv.toggled.connect(self.page3_controller.set_record_csv); self.btn_export_page3_csv.clicked.connect(self.export_page3_csv); self.tbl_page3.itemSelectionChanged.connect(self.on_page3_row_selected); self.page3_slider.valueChanged.connect(self.on_page3_slider_changed)
        self.page3_controller.tx_frame.connect(self.on_page3_tx_frame); self.page3_controller.row_started.connect(self.on_page3_row_started); self.page3_controller.row_updated.connect(self.on_page3_row_updated); self.page3_controller.row_finished.connect(self.on_page3_row_finished); self.page3_controller.distribution_updated.connect(self.on_page3_distribution_updated); self.page3_controller.log_message.connect(lambda s:self._raw_text(f"[PAGE3] {s}"))
        self.btn_page4_use_page3.clicked.connect(self.page4_use_page3_data); self.btn_page4_load_csv.clicked.connect(self.page4_load_csv); self.btn_page4_analyze.clicked.connect(self.page4_analyze); self.btn_page4_clear.clicked.connect(self.page4_clear); self.btn_page4_export.clicked.connect(self.page4_export)

    def refresh_pcan_channels(self):
        current_val=self.cmb_channel.currentData() if hasattr(self,"cmb_channel") else None; self.cmb_channel.blockSignals(True); self.cmb_channel.clear()
        try: channels=detect_available_pcan_channels(self.pcan if hasattr(self,"pcan") else None)
        except Exception: channels=build_usb_channels()
        if channels:
            for name,val in channels: self.cmb_channel.addItem(name,val)
        else: self.cmb_channel.addItem("No PCAN USB detected",None)
        if current_val is not None:
            try:
                old_val=to_int(current_val)
                for i in range(self.cmb_channel.count()):
                    if self.cmb_channel.itemData(i) is not None and to_int(self.cmb_channel.itemData(i))==old_val: self.cmb_channel.setCurrentIndex(i); break
            except Exception: pass
        self.cmb_channel.blockSignals(False)

    def _now(self): return QDateTime.currentDateTime().toString("HH:mm:ss.zzz")
    def _raw_enqueue(self,direction,can_id,dlc,data,is_ext,note=""):
        line=f"[{self._now()}] {direction} ID=0x{can_id:08X} DLC={dlc} DATA="+" ".join(f"{b:02X}" for b in data[:dlc]); self._raw_queue.append((line+(f"  {note}" if note else ""),is_ext))
    def _raw_text(self,text): self._raw_queue.append((f"[{self._now()}] {text}",False))
    def _parse_hex_bytes(self,s): return bytes(int(p,16)&0xFF for p in s.strip().replace(","," ").split() if p)
    def _selected_signals(self): return [n for n,c in self.chk_map.items() if c.isChecked()]
    def _selected_signals_set(self): return set(self._selected_signals())

    def on_select_all_toggled(self,checked):
        if self._bulk_checking:return
        self._bulk_checking=True
        try:
            for cb in self.chk_map.values(): cb.setChecked(checked)
        finally:self._bulk_checking=False

    def apply_id_filter(self):
        t=self.le_id_filter.text().strip()
        if not t:self._id_filter_set=None; return
        try:self._id_filter_set=set(int(p,16) for p in t.replace(","," ").split())
        except Exception as e: QMessageBox.warning(self,"ID Filter",str(e))
    def clear_id_filter(self): self._id_filter_set=None; self.le_id_filter.clear()
    def toggle_connect(self): self.do_disconnect() if self.connected else self.do_connect()

    def do_connect(self):
        try:
            channel_data=self.cmb_channel.currentData()
            if channel_data is None:
                QMessageBox.information(self,"PCAN","目前沒有偵測到可用的 PCAN USB，請確認裝置已連接後按『重新偵測』。"); return
            self.channel_val=to_int(channel_data); baud_val=to_int(self.cmb_baud.currentData()); st=to_int(self.pcan.Initialize(self.channel_val,baud_val))
            if st!=PCAN_ERROR_OK: QMessageBox.warning(self,"PCAN",f"Initialize 失敗，錯誤碼: 0x{st:X}"); return
            self.connected=True; self.btn_connect.setText("斷線"); self.lbl_status.setText("Status: Connected"); self.rx_thread=PcanRxThread(self.pcan,self.channel_val); self.rx_thread.frame_received.connect(self.on_frame); self.rx_thread.start_rx(); self.on_page2_selection_changed()
        except Exception as e: QMessageBox.critical(self,"PCAN",f"連線失敗：{e}")

    def do_disconnect(self):
        for i in range(3): self.btn_periodic[i].setChecked(False); self.cmd_periodic[i].stop()
        self.page2_periodic.stop(); self.page3_controller.stop(); self._burst_queue.clear(); self._burst_timer.stop()
        if self.rx_thread:self.rx_thread.stop_rx(); self.rx_thread=None
        try:self.pcan.Uninitialize(self.channel_val)
        except Exception:pass
        self.connected=False; self.btn_connect.setText("連線"); self.lbl_status.setText("Status: Disconnected")

    @Slot(object)
    def on_frame(self,frame):
        self._frame_queue.append(frame); is_ext=(to_int(frame.msgtype)&PCAN_MESSAGE_EXTENDED)!=0
        if self._id_filter_set is None or frame.can_id in self._id_filter_set:self._raw_enqueue("RX",frame.can_id,frame.dlc,frame.data,is_ext)

    def flush_ui(self):
        self._decode_frames(3000)
        if self.btn_pause.isChecked(): return
        for _ in range(min(700,len(self._raw_queue))):
            line,is_ext=self._raw_queue.popleft()
            if (is_ext and self.chk_show_ext.isChecked()) or ((not is_ext) and self.chk_show_std.isChecked()) or "[PAGE3]" in line:self.txt_raw.appendPlainText(line)

    def _decode_frames(self,limit=3000):
        selected=self._selected_signals_set()
        for _ in range(min(limit,len(self._frame_queue))): self._try_decode(self._frame_queue.popleft(),selected)

    def _send_can(self,can_id,data,is_ext,log_tx=True):
        if not self.connected:return False
        msg=TPCANMsg(); msg.ID=to_int(can_id); msg.LEN=min(len(data),8); msg.MSGTYPE=PCAN_MESSAGE_EXTENDED if is_ext else PCAN_MESSAGE_STANDARD
        for i in range(8):msg.DATA[i]=0
        for i,b in enumerate(data[:8]):msg.DATA[i]=int(b)&0xFF
        st=to_int(self.pcan.Write(self.channel_val,msg)); return st==PCAN_ERROR_OK

    def send_cmd_once(self,idx):
        try:self._send_can(int(self.cmd_id[idx].text(),16),self._parse_hex_bytes(self.cmd_data[idx].text())[:self.cmd_len[idx].value()],self.cmd_is_ext[idx].currentText()=="EXT")
        except Exception as e:self._raw_text(str(e))
    def toggle_cmd_periodic(self,idx,checked): self.cmd_periodic[idx].start() if checked else self.cmd_periodic[idx].stop()

    def on_page2_selection_changed(self):
        if self._bulk_checking:return
        selected=self._selected_signals_set()
        if self.connected and selected:
            if not self.page2_periodic.isActive(): self.page2_periodic.start()
        else:self.page2_periodic.stop()
        self._sync_series(); self._refresh_value_table()

    def page2_send_burst(self):
        selected=self._selected_signals_set(); items=deque()
        if "rsoc" in selected:items.append(("rsoc",RSOC_TX))
        for name,code in EKF_SOC_CODES:
            if name in selected:items.append((name,bytes([code,0x02])))
        if "r0" in selected:items.append(("r0",R0_TX))
        for name,code in EKF_R0_CODES:
            if name in selected:items.append((name,bytes([code,0x02])))
        self._burst_queue.extend(items)
        if items and not self._burst_timer.isActive():self._burst_timer.start()
    def _burst_send_next(self):
        if not self._burst_queue:self._burst_timer.stop();return
        _,payload=self._burst_queue.popleft();self._send_can(TX_REQ_ID,payload,True)

    def _try_decode(self,frame,selected):
        if frame.can_id!=RX_RSP_ID:return
        d=(frame.data+b"\x00"*8)[:8]; dlc=frame.dlc; t=time.monotonic()-self._t0
        if "rsoc" in selected and dlc>=6 and d[0]==0x05 and d[1]==0:self._push_value("rsoc",t,struct.unpack("<f",bytes(d[2:6]))[0])
        if dlc>=4 and d[1]==0x02:
            code=d[0]
            if code in CODE_TO_SOC:
                if CODE_TO_SOC[code] in selected:self._push_value(CODE_TO_SOC[code],t,u16_le(d[2],d[3])/10.0)
                rn=CODE_TO_R0.get(code)
                if rn in selected and dlc>=6:self._push_value(rn,t,float(u16_le(d[4],d[5])))
            if code==0x41 and "r0" in selected:self._push_value("r0",t,float(u16_le(d[2],d[3])))
            if code in PAGE3_FIELD_BY_CODE:self.page3_controller.on_rx_frame(frame.can_id,d)
    def _push_value(self,name,t,val):self.latest_value[name]=val;self.data_buf[name].append((t,val))

    def _sync_series(self):
        selected=self._selected_signals_set()
        for name in list(self.series_map):
            if name not in selected:self.chart.removeSeries(self.series_map.pop(name))
        for name in selected:
            if name not in self.series_map:
                s=QLineSeries();s.setName(name);self.chart.addSeries(s);s.attachAxis(self.axis_x);s.attachAxis(self.axis_y);self.series_map[name]=s
    def _refresh_value_table(self):
        selected=self._selected_signals();self.tbl_values.setRowCount(len(selected))
        for r,name in enumerate(selected):self.tbl_values.setItem(r,0,QTableWidgetItem(name));self.tbl_values.setItem(r,1,QTableWidgetItem(str(self.latest_value.get(name,""))))
    def refresh_chart(self):
        for name,s in self.series_map.items():s.replace([QPointF(t,float(v)) for t,v in self.data_buf[name]])
        self._refresh_value_table()

    @Slot(int,bytes,bool)
    def on_page3_tx_frame(self,can_id,payload,is_ext):self._send_can(can_id,payload,is_ext)
    @Slot(int,str)
    def on_page3_row_started(self,round_idx,time_label):
        self.tbl_page3.insertRow(self.tbl_page3.rowCount());self.tbl_page3.setItem(round_idx,0,QTableWidgetItem(time_label));self.page3_slider.setMaximum(round_idx)
    @Slot(int,dict)
    def on_page3_row_updated(self,round_idx,values):
        for c,name in enumerate(PAGE3_HEADERS[1:],1):self.tbl_page3.setItem(round_idx,c,QTableWidgetItem(f"{values[name]:.6f}" if name in values else ""))
    @Slot(int,dict)
    def on_page3_row_finished(self,round_idx,values):self.on_page3_row_updated(round_idx,values)
    @Slot(dict)
    def on_page3_distribution_updated(self,values):self.page3_last_values=dict(values)

    def on_page3_row_selected(self):
        rows=self.tbl_page3.selectionModel().selectedRows()
        if rows:self.page3_selected_index=rows[0].row();self.refresh_page3_chart()
    def on_page3_slider_changed(self,value):self.page3_selected_index=value;self.refresh_page3_chart()
    def _gaussian_points(self,mu,var,xmin,xmax,n=200):
        var=max(var,1e-9);sigma=math.sqrt(var);return [QPointF(xmin+i*(xmax-xmin)/(n-1),(1/(sigma*math.sqrt(2*math.pi)))*math.exp(-.5*((xmin+i*(xmax-xmin)/(n-1)-mu)/sigma)**2)) for i in range(n)]
    def refresh_page3_chart(self):
        row=self.page3_controller.get_row(self.page3_selected_index);src=row.values if row else self.page3_last_values
        if not src:return
        soc=float(src.get("soc",.5));soc_true=float(src.get("soc_true",soc));p00=max(abs(float(src.get("p00",.01))),1e-9);q00=max(abs(float(src.get("q00",.001))),1e-9);S=max(abs(float(src.get("S",.01))),1e-9);innovation=float(src.get("innovation",0));k0=float(src.get("k0",0));pred_mu=soc-k0*innovation;meas_mu=soc_true;post_mu=soc;pred_var=p00+q00;meas_var=S;post_var=p00;left=min(pred_mu-4*math.sqrt(pred_var),meas_mu-4*math.sqrt(meas_var),post_mu-4*math.sqrt(post_var));right=max(pred_mu+4*math.sqrt(pred_var),meas_mu+4*math.sqrt(meas_var),post_mu+4*math.sqrt(post_var));self.pred_series.replace(self._gaussian_points(pred_mu,pred_var,left,right));self.meas_series.replace(self._gaussian_points(meas_mu,meas_var,left,right));self.post_series.replace(self._gaussian_points(post_mu,post_var,left,right));self.ekf_axis_x.setRange(left,right)
    def clear_page3_data(self):self.page3_controller.clear();self.tbl_page3.setRowCount(0);self.page3_last_values.clear()

    def page4_use_page3_data(self):self.page4_data_rows=self.page3_controller.export_rows();self.lbl_page4_source.setText(f"資料來源：Page3 ({len(self.page4_data_rows)} rows)")
    def page4_load_csv(self):
        path,_=QFileDialog.getOpenFileName(self,"載入 Page3 CSV","","CSV Files (*.csv)")
        if path:
            with open(path,"r",encoding="utf-8-sig",newline="") as f:self.page4_data_rows=list(csv.DictReader(f))
            self.lbl_page4_source.setText(f"資料來源：{os.path.basename(path)}")
    def _page4_num(self,row,key):
        try:return float(row.get(key,""))
        except:return None
    def page4_analyze(self):
        if not self.page4_data_rows:return
        vals=lambda k:[x for r in self.page4_data_rows if (x:=self._page4_num(r,k)) is not None];mean=lambda xs:sum(xs)/len(xs) if xs else 0
        inn=vals("innovation");k0=vals("k0");p00=vals("p00");S=vals("S");errs=[]
        for r in self.page4_data_rows:
            a=self._page4_num(r,"soc");b=self._page4_num(r,"soc_true")
            if a is not None and b is not None:errs.append(a-b)
        summary=[("rows",len(self.page4_data_rows)),("innovation_abs_mean",mean([abs(x) for x in inn])),("k0_mean",mean(k0)),("p00_mean",mean(p00)),("S_mean",mean(S)),("soc_err_abs_mean",mean([abs(x) for x in errs]))]
        self.tbl_page4_summary.setRowCount(len(summary))
        for i,(k,v) in enumerate(summary):self.tbl_page4_summary.setItem(i,0,QTableWidgetItem(k));self.tbl_page4_summary.setItem(i,1,QTableWidgetItem(f"{v:.6f}" if isinstance(v,float) else str(v)))
        self.txt_page4_analysis.setPlainText("\n".join(f"{k} = {v}" for k,v in summary))
    def page4_clear(self):self.page4_data_rows=[];self.tbl_page4_summary.setRowCount(0);self.tbl_page4_advice.setRowCount(0);self.txt_page4_analysis.clear()
    def page4_export(self):
        path,_=QFileDialog.getSaveFileName(self,"匯出分析結果","page4_analysis.txt","Text Files (*.txt)")
        if path:open(path,"w",encoding="utf-8").write(self.txt_page4_analysis.toPlainText())

    def export_csv(self): pass
    def export_page3_csv(self):
        rows=self.page3_controller.export_rows()
        if not rows:return
        path,_=QFileDialog.getSaveFileName(self,"Save Page3 CSV","page3_ekf_table.csv","CSV Files (*.csv)")
        if path:
            w=CsvAutoSplitWriter(path,PAGE3_HEADERS)
            for r in rows:w.write_row(r)
            w.close()
    def clear_data(self):
        self.csv_rows.clear();self.latest_value.clear();[b.clear() for b in self.data_buf.values()];self.clear_page3_data();self.page4_clear()
    def closeEvent(self,event):
        if self.connected:self.do_disconnect()
        super().closeEvent(event)


def main():
    app=QApplication(sys.argv);w=MainWindow();w.show();sys.exit(app.exec())

if __name__=="__main__":main()
