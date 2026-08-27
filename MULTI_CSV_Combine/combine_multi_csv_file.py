import csv
import os
import sys
import traceback
from pathlib import Path

from openpyxl import Workbook


# ============================================================
# 使用者設定
# ============================================================

INPUT_FOLDER = Path("./xls_files")
OUTPUT_FILE = Path("merged_output.xlsx")

# Excel 每張工作表最多 1,048,576 列，包含標題列
EXCEL_MAX_ROWS = 1_048_576

# 每寫入多少筆顯示一次進度
PROGRESS_INTERVAL = 50_000

# 支援常見文字編碼
ENCODINGS = [
    "utf-8-sig",
    "utf-8",
    "cp950",
    "big5",
]

# 可能的分隔符號
SEPARATORS = [
    "\t",
    ",",
    ";",
    "|",
]

# 視為「沒有資料」的內容。
# 注意：只有「整個欄位」全部都是這些內容時才會刪除該欄。
NA_VALUES = {
    "",
    "NA",
    "N/A",
    "NAN",
    "NULL",
    "NONE",
}

# 是否忽略 NA 判斷時的大小寫
NA_CASE_INSENSITIVE = True

# 是否去除儲存格前後空白
STRIP_VALUE = True


# ============================================================
# 基本工具
# ============================================================

def normalize_value(value):
    """整理儲存格文字。"""
    if value is None:
        return ""

    value = str(value)

    if STRIP_VALUE:
        value = value.strip()

    return value


def convert_to_number(value):
    """
    除了第一列 Title/Header 之外，資料儲存格盡可能轉成真正數值。

    例如：
        "123"       -> int 123
        "-45"       -> int -45
        "3.1415"    -> float 3.1415
        "1.2E-3"    -> float 0.0012
        "+10.0"     -> float 10.0

    無法轉成數值的內容仍保留原文字，避免資料遺失。
    NA / 空白則由原本流程轉成空白。
    """
    value = normalize_value(value)

    if value == "":
        return ""

    # 先嘗試整數，避免 123 被寫成 123.0
    try:
        # 含小數點或科學記號時直接走 float
        upper_value = value.upper()
        if "." not in value and "E" not in upper_value:
            return int(value)
    except (ValueError, TypeError):
        pass

    try:
        return float(value)
    except (ValueError, TypeError):
        # 若真的不是數字，例如文字狀態欄，保留原內容
        return value


def is_na_value(value):
    """
    判斷一個值是否視為 NA。

    注意：
    這個函式只負責判斷單一 cell。
    真正刪除欄位時，必須整個欄位每一筆都是 NA 才會刪除。
    """
    value = normalize_value(value)

    if NA_CASE_INSENSITIVE:
        return value.upper() in {x.upper() for x in NA_VALUES}

    return value in NA_VALUES


def detect_encoding(file_path):
    """
    找出可以正常解碼文字檔的編碼。

    只讀取前 1 MB，避免大檔案整份進記憶體。
    """
    sample_size = 1024 * 1024

    for encoding in ENCODINGS:
        try:
            with open(file_path, "r", encoding=encoding, newline="") as f:
                f.read(sample_size)
            return encoding
        except UnicodeDecodeError:
            continue
        except Exception:
            continue

    raise UnicodeError(
        f"無法判斷檔案編碼：{file_path}\n"
        f"已嘗試：{', '.join(ENCODINGS)}"
    )


def detect_separator(file_path, encoding):
    """
    自動判斷 CSV/TXT 的分隔符。
    """
    with open(file_path, "r", encoding=encoding, newline="") as f:
        sample = f.read(64 * 1024)

    if not sample:
        raise ValueError(f"檔案是空的：{file_path}")

    try:
        dialect = csv.Sniffer().sniff(sample, delimiters="".join(SEPARATORS))
        return dialect.delimiter
    except csv.Error:
        pass

    # Sniffer 判斷失敗時，改用第一個非空白行統計
    first_line = ""
    for line in sample.splitlines():
        if line.strip():
            first_line = line
            break

    if not first_line:
        raise ValueError(f"找不到有效內容：{file_path}")

    counts = {sep: first_line.count(sep) for sep in SEPARATORS}
    best_sep = max(counts, key=counts.get)

    if counts[best_sep] <= 0:
        raise ValueError(
            f"無法判斷分隔符號：{file_path}\n"
            f"請確認檔案是否為 CSV / TXT / tab-delimited 文字格式。"
        )

    return best_sep


def get_input_files():
    """
    找出待合併檔案。

    可處理：
      .csv
      .txt
      .tsv
      .xls  （僅限實際內容為文字格式的匯出檔）

    如果 .xls 是真正的 Excel binary BIFF 檔案，本程式不會直接解析。
    """
    if not INPUT_FOLDER.exists():
        raise FileNotFoundError(
            f"找不到輸入資料夾：{INPUT_FOLDER.resolve()}"
        )

    extensions = {".csv", ".txt", ".tsv", ".xls"}

    files = [
        p for p in INPUT_FOLDER.iterdir()
        if p.is_file()
        and p.suffix.lower() in extensions
        and p.resolve() != OUTPUT_FILE.resolve()
        and not p.name.startswith("~$")
    ]

    files.sort(key=lambda x: x.name.lower())

    return files


def inspect_text_file(file_path):
    """
    取得檔案的 encoding、separator 與 header。
    """
    encoding = detect_encoding(file_path)

    if file_path.suffix.lower() == ".tsv":
        separator = "\t"
    else:
        separator = detect_separator(file_path, encoding)

    with open(
        file_path,
        "r",
        encoding=encoding,
        newline="",
        errors="strict",
    ) as f:
        reader = csv.reader(f, delimiter=separator)

        try:
            header = next(reader)
        except StopIteration:
            raise ValueError(f"檔案沒有內容：{file_path}")

    header = [normalize_value(x) for x in header]

    # 避免空 header 導致後續欄位無法識別
    fixed_header = []
    used_names = {}

    for i, name in enumerate(header, start=1):
        if not name:
            name = f"Column_{i}"

        # 相同欄名自動補編號
        if name in used_names:
            used_names[name] += 1
            name = f"{name}_{used_names[name]}"
        else:
            used_names[name] = 1

        fixed_header.append(name)

    return {
        "path": file_path,
        "encoding": encoding,
        "separator": separator,
        "header": fixed_header,
    }


# ============================================================
# 第一階段：掃描欄位
# ============================================================

def scan_columns(file_infos):
    """
    第一遍掃描全部輸入資料。

    目的：
    1. 建立所有檔案的欄位聯集。
    2. 找出哪些欄位至少有一筆「非 NA」資料。
    3. 不把整份資料放進 RAM。

    回傳：
        all_columns:
            所有出現過的欄名，依首次出現順序排列。

        columns_with_data:
            至少有一筆有效資料的欄位 set。

        total_rows:
            掃描到的總資料列數。
    """
    all_columns = []
    known_columns = set()
    columns_with_data = set()
    total_rows = 0

    print("=" * 70)
    print("第一階段：掃描欄位 / 判斷全 NA 欄位")
    print("=" * 70)

    for file_index, info in enumerate(file_infos, start=1):
        file_path = info["path"]
        encoding = info["encoding"]
        separator = info["separator"]
        header = info["header"]

        print(f"\n[{file_index}/{len(file_infos)}] 掃描：{file_path.name}")
        print(f"  encoding : {encoding}")
        print(f"  separator: {repr(separator)}")
        print(f"  columns  : {len(header)}")

        for col in header:
            if col not in known_columns:
                known_columns.add(col)
                all_columns.append(col)

        file_rows = 0

        with open(
            file_path,
            "r",
            encoding=encoding,
            newline="",
            errors="strict",
        ) as f:
            reader = csv.reader(f, delimiter=separator)

            # 跳過 header
            try:
                next(reader)
            except StopIteration:
                continue

            for row in reader:
                file_rows += 1
                total_rows += 1

                # row 太短時視為空值；太長則忽略 header 之外的額外欄位
                for i, col_name in enumerate(header):
                    if col_name in columns_with_data:
                        # 此欄已經確定有有效資料，不必再判斷
                        continue

                    value = row[i] if i < len(row) else ""

                    if not is_na_value(value):
                        columns_with_data.add(col_name)

                if file_rows % PROGRESS_INTERVAL == 0:
                    print(
                        f"\r  已掃描 {file_rows:,} rows",
                        end="",
                        flush=True,
                    )

        if file_rows >= PROGRESS_INTERVAL:
            print()

        print(f"  完成：{file_rows:,} rows")

    return all_columns, columns_with_data, total_rows


# ============================================================
# 第二階段：寫 Excel
# ============================================================

def write_excel(file_infos, output_columns):
    """
    第二遍讀取資料並寫入 Excel。

    使用 openpyxl write_only=True，
    避免大量資料全部留在 RAM。

    當資料超過 Excel 單一工作表限制時，自動建立：
        Sheet1
        Sheet2
        Sheet3
        ...
    """
    print("\n" + "=" * 70)
    print("第二階段：寫入 Excel")
    print("=" * 70)

    wb = Workbook(write_only=True)

    sheet_number = 0
    ws = None
    rows_in_current_sheet = 0
    total_written = 0

    def create_new_sheet():
        nonlocal sheet_number, ws, rows_in_current_sheet

        sheet_number += 1
        ws = wb.create_sheet(title=f"Sheet{sheet_number}")

        # 每一張 sheet 第一列都是 header
        ws.append(output_columns)
        rows_in_current_sheet = 1

        print(f"\n建立 Sheet{sheet_number}")

    create_new_sheet()

    output_index = {name: idx for idx, name in enumerate(output_columns)}

    for file_index, info in enumerate(file_infos, start=1):
        file_path = info["path"]
        encoding = info["encoding"]
        separator = info["separator"]
        header = info["header"]

        print(f"\n[{file_index}/{len(file_infos)}] 寫入：{file_path.name}")

        # 目前檔案的 column -> output column index
        file_to_output = []

        for file_col_idx, col_name in enumerate(header):
            if col_name in output_index:
                file_to_output.append(
                    (file_col_idx, output_index[col_name])
                )

        file_written = 0

        with open(
            file_path,
            "r",
            encoding=encoding,
            newline="",
            errors="strict",
        ) as f:
            reader = csv.reader(f, delimiter=separator)

            try:
                next(reader)
            except StopIteration:
                continue

            for row in reader:
                # Excel Sheet 滿了：開下一張
                if rows_in_current_sheet >= EXCEL_MAX_ROWS:
                    create_new_sheet()

                output_row = [""] * len(output_columns)

                for src_idx, dst_idx in file_to_output:
                    value = row[src_idx] if src_idx < len(row) else ""
                    value = normalize_value(value)

                    # NA cell 統一輸出空白
                    # 若希望保留文字 "NA"，可把下面兩行註解掉。
                    if is_na_value(value):
                        value = ""
                    else:
                        # Header 由 ws.append(output_columns) 單獨寫入；
                        # 其餘資料列盡可能轉成 Excel 真正的數值型別。
                        value = convert_to_number(value)

                    output_row[dst_idx] = value

                ws.append(output_row)

                rows_in_current_sheet += 1
                total_written += 1
                file_written += 1

                if file_written % PROGRESS_INTERVAL == 0:
                    print(
                        f"\r  已寫入 {file_written:,} rows "
                        f"(總計 {total_written:,})",
                        end="",
                        flush=True,
                    )

        if file_written >= PROGRESS_INTERVAL:
            print()

        print(f"  完成：{file_written:,} rows")

    print("\n正在儲存 Excel ...")
    wb.save(OUTPUT_FILE)

    return total_written, sheet_number


# ============================================================
# 主程式
# ============================================================

def main():
    print("=" * 70)
    print("Large File Excel Merge Tool V3 - Numeric Data")
    print("=" * 70)
    print(f"輸入資料夾：{INPUT_FOLDER.resolve()}")
    print(f"輸出檔案  ：{OUTPUT_FILE.resolve()}")

    files = get_input_files()

    if not files:
        print("\n找不到可處理的輸入檔案。")
        print("支援副檔名：.csv / .txt / .tsv / .xls(文字格式)")
        return 1

    print(f"\n找到 {len(files)} 個檔案：")
    for f in files:
        size_mb = f.stat().st_size / (1024 * 1024)
        print(f"  - {f.name} ({size_mb:.2f} MB)")

    # --------------------------------------------------------
    # 分析各檔案格式
    # --------------------------------------------------------
    file_infos = []

    print("\n分析檔案格式 ...")

    for file_path in files:
        try:
            info = inspect_text_file(file_path)
            file_infos.append(info)
        except Exception as e:
            print(f"\n[ERROR] 無法讀取：{file_path}")
            print(f"原因：{e}")
            return 1

    # --------------------------------------------------------
    # PASS 1：找出全 NA 欄
    # --------------------------------------------------------
    all_columns, columns_with_data, scanned_rows = scan_columns(file_infos)

    output_columns = [
        col for col in all_columns
        if col in columns_with_data
    ]

    removed_columns = [
        col for col in all_columns
        if col not in columns_with_data
    ]

    print("\n" + "=" * 70)
    print("欄位掃描結果")
    print("=" * 70)

    print(f"總資料列數      ：{scanned_rows:,}")
    print(f"原始欄位數      ：{len(all_columns):,}")
    print(f"保留欄位數      ：{len(output_columns):,}")
    print(f"刪除全 NA 欄位數：{len(removed_columns):,}")

    if removed_columns:
        print("\n以下欄位因為全部為 NA / 空白而刪除：")
        for col in removed_columns:
            print(f"  - {col}")

    if not output_columns:
        print("\n[ERROR] 所有欄位都沒有有效資料，無法產生 Excel。")
        return 1

    # --------------------------------------------------------
    # PASS 2：輸出 Excel
    # --------------------------------------------------------
    written_rows, sheet_count = write_excel(
        file_infos=file_infos,
        output_columns=output_columns,
    )

    # --------------------------------------------------------
    # 最終確認
    # --------------------------------------------------------
    print("\n" + "=" * 70)
    print("完成")
    print("=" * 70)

    print(f"掃描資料列：{scanned_rows:,}")
    print(f"輸出資料列：{written_rows:,}")
    print(f"輸出欄位數：{len(output_columns):,}")
    print(f"工作表數量：{sheet_count}")
    print(f"輸出位置  ：{OUTPUT_FILE.resolve()}")

    if scanned_rows != written_rows:
        print("\n[WARNING] 掃描列數與輸出列數不同，請檢查輸入檔案。")
        return 2

    print("\n資料列數檢查正常。")
    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except KeyboardInterrupt:
        print("\n\n使用者中止程式。")
        exit_code = 130
    except Exception:
        print("\n發生未預期錯誤：")
        traceback.print_exc()
        exit_code = 1

    sys.exit(exit_code)
