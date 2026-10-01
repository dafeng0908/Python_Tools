import json, re, subprocess, sys
from pathlib import Path
import win32com.client

BASE = Path(__file__).resolve().parent
CFG = json.loads((BASE/"config.json").read_text(encoding="utf-8"))
STATE = BASE/"processed.json"

def load_processed():
    if not STATE.exists():
        return {}
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_processed(d):
    STATE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

def find_url(text):
    m = re.search(r'https?://(?:www\.)?surveycake\.com/s/[A-Za-z0-9_-]+', text or "", re.I)
    return m.group(0) if m else None

outlook = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
inbox = outlook.GetDefaultFolder(6)
items = inbox.Items
items.Sort("[ReceivedTime]", True)

processed = load_processed()
keyword = CFG.get("subject_keyword", "外訂便當").lower()
sender_filter = CFG.get("sender_contains", "").lower().strip()

for i in range(1, min(items.Count, CFG.get("scan_latest", 30)) + 1):
    msg = items.Item(i)
    try:
        subject = str(getattr(msg, "Subject", "") or "")
        sender = str(getattr(msg, "SenderEmailAddress", "") or "")
        body = str(getattr(msg, "Body", "") or "")
        html = str(getattr(msg, "HTMLBody", "") or "")
        entry_id = str(getattr(msg, "EntryID", "") or "")

        if keyword not in subject.lower():
            continue
        if sender_filter and sender_filter not in sender.lower():
            continue
        if not entry_id or entry_id in processed:
            continue

        url = find_url(body) or find_url(html)
        if not url:
            print("符合主旨，但找不到 SurveyCake URL:", subject)
            continue

        print("找到新訂餐 Email:", subject)
        print("SurveyCake:", url)

        result = subprocess.run(
            [sys.executable, str(BASE/"surveycake.py"), "--url", url],
            cwd=str(BASE)
        )

        if result.returncode == 0:
            processed[entry_id] = {
                "subject": subject,
                "sender": sender,
                "survey_url": url,
                "status": "submitted"
            }
            save_processed(processed)
            print("完成並記錄，避免重複提交。")
        else:
            print("SurveyCake 執行失敗，不標記 processed，下次可重試。")
        break
    except Exception as e:
        print("讀取郵件失敗:", e)
