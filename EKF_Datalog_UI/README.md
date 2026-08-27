# EKF Datalog UI

Python + PySide6 PCAN tool for EKF data logging and parameter analysis.

## Features
- Page1: PCAN connect, current-PCAN detection, raw TX/RX monitor, STD/EXT filter, ID filter, three configurable commands.
- Page2: rsoc / EKF SOC / R0 polling, line chart, CSV export with 100 MB file splitting.
- Page3: strict state machine `TX -> WAIT_RX -> WAIT_10MS -> next TX`, 12 EKF signals per round, table, CSV and distribution playback.
- Page4: EKF parameter-tuning analysis based on Page3 data or exported CSV.

## Requirements
```bash
pip install -r requirements.txt
```

Place the official `PCANBasic.py` from PEAK-System beside `EKF_Datalog_UI.py`, then run:

```bash
python EKF_Datalog_UI.py
```

Use a `PCANBasic.py` version that matches the installed PEAK PCAN-Basic runtime/DLL.

## PCAN channel detection
The Page1 channel dropdown reads `PCAN_CHANNEL_CONDITION` when supported by the installed PCAN-Basic version and lists detected USB channels. Use **重新偵測** after plugging or unplugging hardware.

Recommended operating sequence:
1. Disconnect the UI before rescanning channels.
2. Plug or unplug the PCAN adapter.
3. Click **重新偵測**.
4. Select an available channel and connect.

A channel displayed as `occupied` is physically detected but is already in use by another application/process. Close the other PCAN client first if `Initialize` fails on that channel.

## Page3 timing rule
Page3 intentionally waits for the matching RX response before proceeding:

`TX -> matching RX -> wait 10 ms -> next TX`

A new 12-signal round starts no earlier than one second after the previous round started, and never overlaps an unfinished round.
