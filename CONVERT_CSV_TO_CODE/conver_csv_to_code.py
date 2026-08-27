import csv
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path


class CSVFloatFormatter:
    def __init__(self, root):
        self.root = root
        self.root.title("CSV Column → C Float Formatter")
        self.root.geometry("760x620")

        self.csv_path = None
        self.headers = []
        self.rows = []

        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)

        ttk.Button(frame, text="1. 選擇 CSV", command=self.open_csv).grid(
            row=0, column=0, sticky="w"
        )

        self.file_label = ttk.Label(frame, text="尚未選擇檔案")
        self.file_label.grid(row=0, column=1, columnspan=3, sticky="w", padx=10)

        ttk.Label(frame, text="2. 選擇欄位：").grid(
            row=1, column=0, sticky="w", pady=(15, 5)
        )
        self.column_combo = ttk.Combobox(frame, state="readonly", width=45)
        self.column_combo.grid(row=1, column=1, columnspan=2, sticky="w")

        ttk.Label(frame, text="每行資料數：").grid(
            row=2, column=0, sticky="w", pady=5
        )
        self.per_line = tk.IntVar(value=10)
        ttk.Spinbox(frame, from_=1, to=100, textvariable=self.per_line, width=8).grid(
            row=2, column=1, sticky="w"
        )

        ttk.Label(frame, text="小數位數：").grid(row=3, column=0, sticky="w", pady=5)
        self.decimals = tk.IntVar(value=1)
        ttk.Spinbox(frame, from_=0, to=15, textvariable=self.decimals, width=8).grid(
            row=3, column=1, sticky="w"
        )

        self.keep_zero = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frame,
            text="保留前導 0（例如 01.0f）",
            variable=self.keep_zero
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=5)

        self.add_braces = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frame,
            text="加入 C 陣列大括號 { ... }",
            variable=self.add_braces
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=5)

        ttk.Button(frame, text="3. 轉換", command=self.convert).grid(
            row=6, column=0, sticky="w", pady=(15, 8)
        )
        ttk.Button(frame, text="複製結果", command=self.copy_result).grid(
            row=6, column=1, sticky="w", pady=(15, 8)
        )
        ttk.Button(frame, text="儲存 TXT", command=self.save_txt).grid(
            row=6, column=2, sticky="w", pady=(15, 8)
        )

        ttk.Label(frame, text="輸出預覽：").grid(row=7, column=0, sticky="w")

        self.output = tk.Text(frame, wrap="none", height=24)
        self.output.grid(row=8, column=0, columnspan=4, sticky="nsew")

        y_scroll = ttk.Scrollbar(frame, orient="vertical", command=self.output.yview)
        y_scroll.grid(row=8, column=4, sticky="ns")
        self.output.configure(yscrollcommand=y_scroll.set)

        x_scroll = ttk.Scrollbar(frame, orient="horizontal", command=self.output.xview)
        x_scroll.grid(row=9, column=0, columnspan=4, sticky="ew")
        self.output.configure(xscrollcommand=x_scroll.set)

        frame.columnconfigure(3, weight=1)
        frame.rowconfigure(8, weight=1)

    def open_csv(self):
        path = filedialog.askopenfilename(
            title="選擇 CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
        )
        if not path:
            return

        try:
            # utf-8-sig 可處理 Excel 常見 BOM
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.reader(f)
                data = list(reader)

            if not data:
                raise ValueError("CSV 是空的")

            self.headers = data[0]
            self.rows = data[1:]
            self.csv_path = Path(path)

            # 顯示「第幾欄 + 欄位名稱」，避免同名欄位難以辨認
            choices = [
                f"第 {i+1} 欄：{name if name.strip() else '(無欄名)'}"
                for i, name in enumerate(self.headers)
            ]
            self.column_combo["values"] = choices
            if choices:
                self.column_combo.current(0)

            self.file_label.config(text=self.csv_path.name)

        except UnicodeDecodeError:
            # 台灣 Windows/Excel CSV 有時使用 cp950
            try:
                with open(path, "r", encoding="cp950", newline="") as f:
                    reader = csv.reader(f)
                    data = list(reader)

                if not data:
                    raise ValueError("CSV 是空的")

                self.headers = data[0]
                self.rows = data[1:]
                self.csv_path = Path(path)

                choices = [
                    f"第 {i+1} 欄：{name if name.strip() else '(無欄名)'}"
                    for i, name in enumerate(self.headers)
                ]
                self.column_combo["values"] = choices
                self.column_combo.current(0)
                self.file_label.config(text=self.csv_path.name)

            except Exception as e:
                messagebox.showerror("錯誤", str(e))

        except Exception as e:
            messagebox.showerror("錯誤", str(e))

    def convert(self):
        if not self.rows:
            messagebox.showwarning("提醒", "請先選擇 CSV")
            return

        index = self.column_combo.current()
        if index < 0:
            messagebox.showwarning("提醒", "請選擇欄位")
            return

        try:
            per_line = max(1, int(self.per_line.get()))
            decimals = max(0, int(self.decimals.get()))

            raw_values = []
            numeric_values = []

            for row_no, row in enumerate(self.rows, start=2):
                if index >= len(row):
                    continue

                text = row[index].strip()
                if text == "":
                    continue

                try:
                    value = float(text)
                except ValueError:
                    raise ValueError(
                        f"第 {row_no} 列不是有效數值：{text}"
                    )

                raw_values.append(text)
                numeric_values.append(value)

            if not numeric_values:
                raise ValueError("選擇的欄位沒有有效資料")

            # 若保留前導 0，依原資料整數部分的最大寬度決定寬度。
            # 例如 01.0 ~ 100.0，寬度會至少保留原始 2 位；
            # 100.0 不會被截斷。
            int_width = 0
            if self.keep_zero.get():
                for text in raw_values:
                    clean = text.lstrip("+-")
                    integer_part = clean.split(".", 1)[0]
                    int_width = max(int_width, len(integer_part))

            formatted = []
            for value in numeric_values:
                if self.keep_zero.get() and value >= 0:
                    # decimals + 小數點所需寬度
                    total_width = int_width + (1 + decimals if decimals > 0 else 0)
                    s = f"{value:0{total_width}.{decimals}f}"
                elif self.keep_zero.get() and value < 0:
                    abs_value = abs(value)
                    total_width = int_width + (1 + decimals if decimals > 0 else 0)
                    s = "-" + f"{abs_value:0{total_width}.{decimals}f}"
                else:
                    s = f"{value:.{decimals}f}"

                formatted.append(s + "f")

            lines = []
            for i in range(0, len(formatted), per_line):
                chunk = formatted[i:i + per_line]
                lines.append(", ".join(chunk))

            # C initializer 中，跨行也需要逗號
            if len(lines) > 1:
                for i in range(len(lines) - 1):
                    lines[i] += ","

            result = "\n".join(lines)

            if self.add_braces.get():
                result = "{\n    " + result.replace("\n", "\n    ") + "\n};"

            self.output.delete("1.0", tk.END)
            self.output.insert("1.0", result)

        except Exception as e:
            messagebox.showerror("轉換失敗", str(e))

    def copy_result(self):
        text = self.output.get("1.0", tk.END).strip()
        if not text:
            messagebox.showwarning("提醒", "目前沒有輸出內容")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        messagebox.showinfo("完成", "已複製到剪貼簿")

    def save_txt(self):
        text = self.output.get("1.0", tk.END).strip()
        if not text:
            messagebox.showwarning("提醒", "請先轉換資料")
            return

        default_name = "formatted_float.txt"
        if self.csv_path:
            default_name = self.csv_path.stem + "_formatted.txt"

        path = filedialog.asksaveasfilename(
            title="儲存輸出",
            defaultextension=".txt",
            initialfile=default_name,
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )
        if not path:
            return

        with open(path, "w", encoding="utf-8") as f:
            f.write(text + "\n")

        messagebox.showinfo("完成", f"已儲存：\n{path}")


if __name__ == "__main__":
    root = tk.Tk()
    app = CSVFloatFormatter(root)
    root.mainloop()
