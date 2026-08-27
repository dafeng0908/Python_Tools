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

## PCAN channel detection
The Page1 channel dropdown reads `PCAN_CHANNEL_CONDITION` when supported by the installed PCAN-Basic version and lists detected USB channels. Use **重新偵測** after plugging or unplugging hardware.
