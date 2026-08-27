import csv
import struct


def load_intel_hex(filename):
    """
    Parse Intel HEX.

    Return:
        memory[address] = byte

    Supported:
        00 : Data Record
        01 : End Of File
        02 : Extended Segment Address
        04 : Extended Linear Address
    """

    memory = {}
    base_address = 0

    with open(filename, "r", encoding="ascii") as f:

        for line_no, line in enumerate(f, start=1):

            line = line.strip()

            if not line:
                continue

            if not line.startswith(":"):
                raise ValueError(
                    f"Line {line_no}: Invalid Intel HEX record"
                )

            try:
                record = bytes.fromhex(line[1:])
            except ValueError:
                raise ValueError(
                    f"Line {line_no}: Invalid HEX data"
                )

            if len(record) < 5:
                raise ValueError(
                    f"Line {line_no}: Record too short"
                )

            byte_count = record[0]

            offset = (
                (record[1] << 8)
                | record[2]
            )

            record_type = record[3]

            data = record[
                4:4 + byte_count
            ]

            # ==================================================
            # Type 00 : Data Record
            # ==================================================

            if record_type == 0x00:

                absolute_address = (
                    base_address + offset
                )

                for i, value in enumerate(data):

                    memory[
                        absolute_address + i
                    ] = value

            # ==================================================
            # Type 01 : End Of File
            # ==================================================

            elif record_type == 0x01:
                break

            # ==================================================
            # Type 02 : Extended Segment Address
            # ==================================================

            elif record_type == 0x02:

                if len(data) != 2:
                    raise ValueError(
                        f"Line {line_no}: "
                        f"Invalid Type 02 record"
                    )

                segment = (
                    (data[0] << 8)
                    | data[1]
                )

                base_address = segment << 4

            # ==================================================
            # Type 04 : Extended Linear Address
            # ==================================================

            elif record_type == 0x04:

                if len(data) != 2:
                    raise ValueError(
                        f"Line {line_no}: "
                        f"Invalid Type 04 record"
                    )

                upper = (
                    (data[0] << 8)
                    | data[1]
                )

                base_address = upper << 16

            # Other record types ignored


    return memory


# ==============================================================
# Find continuous memory blocks
# ==============================================================

def find_continuous_blocks(memory):

    if not memory:
        return []

    addresses = sorted(memory.keys())

    blocks = []

    start = addresses[0]
    previous = addresses[0]

    for address in addresses[1:]:

        # Address discontinuity
        if address != previous + 1:

            blocks.append(
                (start, previous)
            )

            start = address

        previous = address

    # Last block
    blocks.append(
        (start, previous)
    )

    return blocks


# ==============================================================
# Select Float Data Block
# ==============================================================

def find_float_data_range(memory):
    """
    Automatically find continuous data blocks.

    The largest continuous block whose length is >= 4 bytes
    is selected as the float data region.
    """

    blocks = find_continuous_blocks(memory)

    valid_blocks = []

    print()
    print("Detected continuous data blocks:")
    print("----------------------------------------")

    for start, end in blocks:

        size = end - start + 1

        print(
            f"0x{start:08X} - "
            f"0x{end:08X} "
            f"({size} bytes)"
        )

        if size >= 4:
            valid_blocks.append(
                (start, end)
            )

    if not valid_blocks:
        raise ValueError(
            "No valid data block found."
        )

    # ----------------------------------------------------------
    # Select largest continuous block
    # ----------------------------------------------------------

    start_address, end_address = max(
        valid_blocks,
        key=lambda block:
            block[1] - block[0] + 1
    )

    print("----------------------------------------")
    print("Selected data block:")
    print(
        f"START = 0x{start_address:08X}"
    )
    print(
        f"END   = 0x{end_address:08X}"
    )
    print("----------------------------------------")

    return start_address, end_address


# ==============================================================
# Export Float CSV
# ==============================================================

def export_float_csv(
    hex_filename,
    csv_filename
):

    print()
    print("========================================")
    print("Intel HEX -> Float CSV")
    print("========================================")

    print(
        f"HEX File : {hex_filename}"
    )

    print(
        f"CSV File : {csv_filename}"
    )

    # ==========================================================
    # Load HEX
    # ==========================================================

    memory = load_intel_hex(
        hex_filename
    )

    if not memory:
        raise ValueError(
            "HEX file contains no data."
        )

    print(
        f"Loaded bytes : {len(memory)}"
    )

    # ==========================================================
    # Automatically find Start / End
    # ==========================================================

    start_address, end_address = (
        find_float_data_range(memory)
    )

    total_bytes = (
        end_address
        - start_address
        + 1
    )

    total_float = (
        total_bytes // 4
    )

    print(
        f"Data bytes : {total_bytes}"
    )

    print(
        f"Float count: {total_float}"
    )

    # ==========================================================
    # Check alignment
    # ==========================================================

    remaining = (
        total_bytes % 4
    )

    if remaining != 0:

        print()
        print(
            "WARNING:"
        )

        print(
            f"Data size is not multiple of 4."
        )

        print(
            f"Remaining bytes = {remaining}"
        )

        print(
            "Last incomplete float will be ignored."
        )

    # ==========================================================
    # Write CSV
    # ==========================================================

    with open(
        csv_filename,
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as f:

        writer = csv.writer(f)

        # ------------------------------------------------------
        # Header
        # ------------------------------------------------------

        writer.writerow([
            "序列號",
            "位置",
            "Raw Data",
            "Float"
        ])

        # ------------------------------------------------------
        # Data
        # ------------------------------------------------------

        address = start_address
        index = 1

        while address + 3 <= end_address:

            # Check 4 bytes
            if not all(
                addr in memory
                for addr in range(
                    address,
                    address + 4
                )
            ):

                print(
                    f"Missing data: "
                    f"0x{address:08X}"
                )

                address += 4
                continue

            # ==================================================
            # Read raw bytes
            # ==================================================

            raw_bytes = bytes([
                memory[address],
                memory[address + 1],
                memory[address + 2],
                memory[address + 3]
            ])

            # ==================================================
            # Raw data
            # ==================================================

            raw_string = (
                raw_bytes.hex().upper()
            )

            # ==================================================
            # Little Endian Float32
            # ==================================================

            float_value = struct.unpack(
                "<f",
                raw_bytes
            )[0]

            # ==================================================
            # CSV
            # ==================================================

            writer.writerow([
                index,
                f"0x{address:08X}",
                raw_string,
                float_value
            ])

            index += 1
            address += 4

    # ==========================================================
    # Done
    # ==========================================================

    print()
    print("========================================")
    print("Done")
    print("========================================")

    print(
        f"START ADDRESS : "
        f"0x{start_address:08X}"
    )

    print(
        f"END ADDRESS   : "
        f"0x{end_address:08X}"
    )

    print(
        f"FLOAT COUNT   : "
        f"{index - 1}"
    )

    print(
        f"OUTPUT        : "
        f"{csv_filename}"
    )

    print("========================================")


# ==============================================================
# User Settings
# ==============================================================

HEX_FILE = "datalog_soc.hex"

CSV_FILE = "datalog_soc_float.csv"


# ==============================================================
# Main
# ==============================================================

if __name__ == "__main__":

    export_float_csv(
        HEX_FILE,
        CSV_FILE
    )
