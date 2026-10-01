from playwright.sync_api import sync_playwright
import csv, argparse, sys
from pathlib import Path

BASE = Path(__file__).resolve().parent

def subject(page,n):
    x=page.locator(f'[aria-label="subject"]:has([aria-label="subject visual number"][data-vn="{n}"])')
    if x.count()!=1:
        raise RuntimeError(f"第{n}題定位失敗")
    return x

ap=argparse.ArgumentParser()
ap.add_argument("--url", required=True)
args=ap.parse_args()

with open(BASE/"form_data.csv","r",encoding="utf-8-sig",newline="") as f:
    row=next(csv.DictReader(f))

try:
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=False)
        page=browser.new_page(viewport={"width":1400,"height":950})
        page.goto(args.url,wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        subject(page,1).locator(f'[data-qa="option-{row["company"]}"]').click()
        subject(page,2).locator("input[data-subject-answer]").fill(row["employee_id"])
        subject(page,3).locator("input[data-subject-answer]").fill(row["name"])
        subject(page,4).locator("input[data-subject-answer]").fill(row["email"])

        opts=subject(page,5).locator("[data-subject-option-id]")
        if opts.count()<1:
            raise RuntimeError("第5題沒有便當選項")
        print("選擇:", opts.first.inner_text().strip())
        opts.first.click()

        submit=page.locator('[aria-label="submit"] button')
        if submit.count()!=1:
            raise RuntimeError("送出按鈕定位失敗")
        submit.click()
        page.wait_for_timeout(3500)

        if submit.is_visible():
            raise RuntimeError("送出後仍看到送出按鈕，未確認提交成功")

        print("SurveyCake 已提交。")
        browser.close()
    sys.exit(0)
except Exception as e:
    print("ERROR:",e)
    sys.exit(1)
