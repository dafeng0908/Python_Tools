import csv
import os
import sys
import traceback
from pathlib import Path

from openpyxl import Workbook

INPUT_FOLDER = Path("./xls_files")
OUTPUT_FILE = Path("merged_output.xlsx")
EXCEL_MAX_ROWS = 1_048_576
PROGRESS_INTERVAL = 50_000
ENCODINGS = ["utf-8-sig", "utf-8", "cp950", "big5"]
SEPARATORS = ["\t", ",", ";", "|"]
NA_VALUES = {"", "NA", "N/A", "NAN", "NULL", "NONE"}
NA_CASE_INSENSITIVE = True
STRIP_VALUE = True

def normalize_value(value):
    if value is None:
        return ""
    value = str(value)
    return value.strip() if STRIP_VALUE else value

def convert_to_number(value):
    """Convert non-header cells to real Excel numeric values when possible."""
    value = normalize_value(value)
    if value == "":
        return ""
    try:
        upper_value = value.upper()
        if "." not in value and "E" not in upper_value:
            return int(value)
    except (ValueError, TypeError):
        pass
    try:
        return float(value)
    except (ValueError, TypeError):
        return value

def is_na_value(value):
    value = normalize_value(value)
    if NA_CASE_INSENSITIVE:
        return value.upper() in {x.upper() for x in NA_VALUES}
    return value in NA_VALUES

def detect_encoding(file_path):
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
    raise UnicodeError(f"無法判斷檔案編碼：{file_path}\n已嘗試：{', '.join(ENCODINGS)}")

def detect_separator(file_path, encoding):
    with open(file_path, "r", encoding=encoding, newline="") as f:
        sample = f.read(64 * 1024)
    if not sample:
        raise ValueError(f"檔案是空的：{file_path}")
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters="".join(SEPARATORS))
        return dialect.delimiter
    except csv.Error:
        pass
    first_line = next((line for line in sample.splitlines() if line.strip()), "")
    if not first_line:
        raise ValueError(f"找不到有效內容：{file_path}")
    counts = {sep: first_line.count(sep) for sep in SEPARATORS}
    best_sep = max(counts, key=counts.get)
    if counts[best_sep] <= 0:
        raise ValueError(f"無法判斷分隔符號：{file_path}")
    return best_sep

def get_input_files():
    if not INPUT_FOLDER.exists():
        raise FileNotFoundError(f"找不到輸入資料夾：{INPUT_FOLDER.resolve()}")
    extensions = {".csv", ".txt", ".tsv", ".xls"}
    files = [p for p in INPUT_FOLDER.iterdir()
             if p.is_file() and p.suffix.lower() in extensions
             and p.resolve() != OUTPUT_FILE.resolve()
             and not p.name.startswith("~$")]
    files.sort(key=lambda x: x.name.lower())
    return files

def inspect_text_file(file_path):
    encoding = detect_encoding(file_path)
    separator = "\t" if file_path.suffix.lower() == ".tsv" else detect_separator(file_path, encoding)
    with open(file_path, "r", encoding=encoding, newline="", errors="strict") as f:
        reader = csv.reader(f, delimiter=separator)
        try:
            header = next(reader)
        except StopIteration:
            raise ValueError(f"檔案沒有內容：{file_path}")
    header = [normalize_value(x) for x in header]
    fixed_header, used_names = [], {}
    for i, name in enumerate(header, start=1):
        if not name:
            name = f"Column_{i}"
        if name in used_names:
            used_names[name] += 1
            name = f"{name}_{used_names[name]}"
        else:
            used_names[name] = 1
        fixed_header.append(name)
    return {"path": file_path, "encoding": encoding, "separator": separator, "header": fixed_header}

def scan_columns(file_infos):
    all_columns, known_columns, columns_with_data = [], set(), set()
    total_rows = 0
    print("=" * 70)
    print("第一階段：掃描欄位 / 判斷全 NA 欄位")
    print("=" * 70)
    for file_index, info in enumerate(file_infos, start=1):
        file_path, encoding, separator, header = info["path"], info["encoding"], info["separator"], info["header"]
        print(f"\n[{file_index}/{len(file_infos)}] 掃描：{file_path.name}")
        for col in header:
            if col not in known_columns:
                known_columns.add(col)
                all_columns.append(col)
        file_rows = 0
        with open(file_path, "r", encoding=encoding, newline="", errors="strict") as f:
            reader = csv.reader(f, delimiter=separator)
            try:
                next(reader)
            except StopIteration:
                continue
            for row in reader:
                file_rows += 1
                total_rows += 1
                for i, col_name in enumerate(header):
                    if col_name in columns_with_data:
                        continue
                    value = row[i] if i < len(row) else ""
                    if not is_na_value(value):
                        columns_with_data.add(col_name)
                if file_rows % PROGRESS_INTERVAL == 0:
                    print(f"\r  已掃描 {file_rows:,} rows", end="", flush=True)
        if file_rows >= PROGRESS_INTERVAL:
            print()
        print(f"  完成：{file_rows:,} rows")
    return all_columns, columns_with_data, total_rows

def write_excel(file_infos, output_columns):
    print("\n" + "=" * 70)
    print("第二階段：寫入 Excel")
    print("=" * 70)
    wb = Workbook(write_only=True)
    sheet_number, ws, rows_in_current_sheet, total_written = 0, None, 0, 0

    def create_new_sheet():
        nonlocal sheet_number, ws, rows_in_current_sheet
        sheet_number += 1
        ws = wb.create_sheet(title=f"Sheet{sheet_number}")
        ws.append(output_columns)
        rows_in_current_sheet = 1
        print(f"\n建立 Sheet{sheet_number}")

    create_new_sheet()
    output_index = {name: idx for idx, name in enumerate(output_columns)}

    for file_index, info in enumerate(file_infos, start=1):
        file_path, encoding, separator, header = info["path"], info["encoding"], info["separator"], info["header"]
        print(f"\n[{file_index}/{len(file_infos)}] 寫入：{file_path.name}")
        file_to_output = [(i, output_index[col]) for i, col in enumerate(header) if col in output_index]
        file_written = 0
        with open(file_path, "r", encoding=encoding, newline="", errors="strict") as f:
            reader = csv.reader(f, delimiter=separator)
            try:
                next(reader)
            except StopIteration:
                continue
            for row in reader:
                if rows_in_current_sheet >= EXCEL_MAX_ROWS:
                    create_new_sheet()
                output_row = [""] * len(output_columns)
                for src_idx, dst_idx in file_to_output:
                    value = normalize_value(row[src_idx] if src_idx < len(row) else "")
                    if is_na_value(value):
                        value = ""
                    else:
                        value = convert_to_number(value)
                    output_row[dst_idx] = value
                ws.append(output_row)
                rows_in_current_sheet += 1
                total_written += 1
                file_written += 1
                if file_written % PROGRESS_INTERVAL == 0:
                    print(f"\r  已寫入 {file_written:,} rows (總計 {total_written:,})", end="", flush=True)
        if file_written >= PROGRESS_INTERVAL:
            print()
        print(f"  完成：{file_written:,} rows")

    print("\n正在儲存 Excel ...")
    wb.save(OUTPUT_FILE)
    return total_written, sheet_number

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
        print(f"  - {f.name} ({f.stat().st_size / (1024 * 1024):.2f} MB)")
    file_infos = []
    print("\n分析檔案格式 ...")
    for file_path in files:
        try:
            file_infos.append(inspect_text_file(file_path))
        except Exception as e:
            print(f"\n[ERROR] 無法讀取：{file_path}")
            print(f"原因：{e}")
            return 1
    all_columns, columns_with_data, scanned_rows = scan_columns(file_infos)
    output_columns = [col for col in all_columns if col in columns_with_data]
    removed_columns = [col for col in all_columns if col not in columns_with_data]
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
    written_rows, sheet_count = write_excel(file_infos, output_columns)
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
