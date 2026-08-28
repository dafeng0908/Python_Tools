#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

MAGIC = b"PQCSIG04"
FORMAT_VERSION = 4
DOMAIN = b"STM32-PQC-FW-SIGN-v4\x00"

ALG_IDS = {
    "ML-DSA-44": 44,
    "ML-DSA-65": 65,
    "ML-DSA-87": 87,
}
ALG_NAMES = {value: key for key, value in ALG_IDS.items()}

TLV_PUBLIC_KEY = 0x0001
TLV_SIGNATURE = 0x0002
TLV_BUILD_INFO = 0x0003

# 8s + 2H + 11I + 2x32-byte digests = 120 bytes
HEADER_FORMAT = "<8sHHIIIIIIIIIII32s32s"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
TLV_HEADER_FORMAT = "<HHI"
TLV_HEADER_SIZE = struct.calcsize(TLV_HEADER_FORMAT)


class ToolError(RuntimeError):
    pass


def run_command(command: List[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode != 0:
        output = result.stdout.decode(errors="replace")
        raise ToolError(f"$ {subprocess.list2cmdline(command)}\n{output}")
    return result


def locate_openssl(explicit_path: str | None) -> str:
    candidates: list[str] = []
    if explicit_path:
        candidates.append(explicit_path)

    from_path = shutil.which("openssl")
    if from_path:
        candidates.append(from_path)

    candidates.extend([
        r"C:\Program Files\OpenSSL-Win64\bin\openssl.exe",
        r"C:\Program Files\OpenSSL-Win32\bin\openssl.exe",
        r"C:\Program Files\Git\usr\bin\openssl.exe",
    ])

    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            result = subprocess.run(
                [candidate, "version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
        except OSError:
            continue
        if result.returncode == 0:
            return candidate

    raise ToolError("找不到可執行的 openssl。請在 GUI 指定 OpenSSL 3.5+ 的 openssl.exe。")


def check_openssl(executable: str | None) -> dict:
    openssl = locate_openssl(executable)
    version_output = run_command([openssl, "version"]).stdout.decode(errors="replace").strip()

    parts = version_output.split()
    if len(parts) < 2 or parts[0].lower() != "openssl":
        raise ToolError(f"無法識別 OpenSSL 版本：{version_output}")

    try:
        major, minor = [int(x) for x in parts[1].split(".")[:2]]
    except (ValueError, IndexError) as exc:
        raise ToolError(f"無法解析 OpenSSL 版本：{version_output}") from exc

    if (major, minor) < (3, 5):
        raise ToolError(f"需要 OpenSSL 3.5 以上，目前為：{version_output}")

    algorithm_output = run_command(
        [openssl, "list", "-signature-algorithms"]
    ).stdout.decode(errors="replace")

    enabled = [
        name for name in ALG_IDS
        if name.upper() in algorithm_output.upper()
    ]

    if not enabled:
        raise ToolError("目前的 OpenSSL build 未提供 ML-DSA。")

    return {
        "executable": openssl,
        "version": version_output,
        "enabled_algorithms": enabled,
    }


def parse_int(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError as exc:
        raise ToolError(f"無效數值：{value}") from exc


def parse_hex(path: Path) -> Tuple[Dict[int, int], List[str]]:
    memory: Dict[int, int] = {}
    upper_address = 0
    extra_records: List[str] = []

    for line_number, raw_line in enumerate(path.read_text(encoding="ascii").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise ToolError(f"HEX 第 {line_number} 行缺少 ':'")

        try:
            record = bytes.fromhex(line[1:])
        except ValueError as exc:
            raise ToolError(f"HEX 第 {line_number} 行包含無效十六進位資料") from exc

        if len(record) < 5 or len(record) != record[0] + 5:
            raise ToolError(f"HEX 第 {line_number} 行長度錯誤")
        if sum(record) & 0xFF:
            raise ToolError(f"HEX 第 {line_number} 行 checksum 錯誤")

        size = record[0]
        address = (record[1] << 8) | record[2]
        record_type = record[3]
        data = record[4:4 + size]

        if record_type == 0x00:
            absolute_address = upper_address + address
            for offset, value in enumerate(data):
                memory[absolute_address + offset] = value
        elif record_type == 0x01:
            break
        elif record_type == 0x02:
            upper_address = int.from_bytes(data, "big") << 4
        elif record_type == 0x04:
            upper_address = int.from_bytes(data, "big") << 16
        elif record_type in (0x03, 0x05):
            extra_records.append(line)

    if not memory:
        raise ToolError("HEX 不包含任何資料記錄")
    return memory, extra_records


def make_hex_record(address: int, record_type: int, data: bytes) -> str:
    body = bytes([
        len(data),
        (address >> 8) & 0xFF,
        address & 0xFF,
        record_type,
    ]) + data
    checksum = (-sum(body)) & 0xFF
    return ":" + (body + bytes([checksum])).hex().upper()


def write_hex(path: Path, memory: Dict[int, int], extra_records: Iterable[str] = ()) -> None:
    lines: List[str] = []
    current_upper = None
    sorted_addresses = sorted(memory)
    index = 0

    while index < len(sorted_addresses):
        start = sorted_addresses[index]
        upper = start >> 16

        if upper != current_upper:
            lines.append(make_hex_record(0, 0x04, upper.to_bytes(2, "big")))
            current_upper = upper

        chunk_start = start
        chunk = bytearray([memory[start]])
        index += 1
        expected = start + 1

        while (
            index < len(sorted_addresses)
            and sorted_addresses[index] == expected
            and len(chunk) < 16
            and (sorted_addresses[index] >> 16) == upper
        ):
            chunk.append(memory[sorted_addresses[index]])
            index += 1
            expected += 1

        lines.append(make_hex_record(chunk_start & 0xFFFF, 0x00, bytes(chunk)))

    lines.extend(extra_records)
    lines.append(make_hex_record(0, 0x01, b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def get_image_bytes(
    memory: Dict[int, int],
    image_start: int,
    image_end: int,
    metadata_address: int,
    metadata_size: int,
) -> bytes:
    if image_start >= image_end:
        raise ToolError("Image Start 必須小於 Image End")

    metadata_end = metadata_address + metadata_size
    if image_start < metadata_end and metadata_address < image_end:
        raise ToolError("Image 範圍不可與 Metadata 區域重疊")

    return bytes(memory.get(address, 0xFF) for address in range(image_start, image_end))


def encode_tlv(tlv_type: int, value: bytes, flags: int = 0) -> bytes:
    return struct.pack(TLV_HEADER_FORMAT, tlv_type, flags, len(value)) + value


def decode_tlvs(blob: bytes) -> dict[int, bytes]:
    offset = 0
    values: dict[int, bytes] = {}
    while offset < len(blob):
        remaining = blob[offset:]
        if all(x == 0xFF for x in remaining):
            break
        if len(remaining) < TLV_HEADER_SIZE:
            raise ToolError("TLV header 不完整")
        tlv_type, _flags, length = struct.unpack(
            TLV_HEADER_FORMAT,
            remaining[:TLV_HEADER_SIZE],
        )
        offset += TLV_HEADER_SIZE
        if offset + length > len(blob):
            raise ToolError("TLV value 超出 Metadata 範圍")
        if tlv_type in values:
            raise ToolError(f"重複 TLV type：0x{tlv_type:04X}")
        values[tlv_type] = blob[offset:offset + length]
        offset += length
    return values


@dataclass(frozen=True)
class ManifestHeader:
    algorithm_id: int
    image_start: int
    image_end: int
    metadata_address: int
    metadata_size: int
    payload_size: int
    tlv_size: int
    signature_size: int
    public_key_size: int
    flags: int
    firmware_digest: bytes
    public_key_digest: bytes

    def pack(self) -> bytes:
        if len(self.firmware_digest) != 32:
            raise ToolError("Firmware digest 必須為 32 bytes")
        if len(self.public_key_digest) != 32:
            raise ToolError("Public-key digest 必須為 32 bytes")

        return struct.pack(
            HEADER_FORMAT,
            MAGIC,
            FORMAT_VERSION,
            HEADER_SIZE,
            self.algorithm_id,
            self.image_start,
            self.image_end,
            self.metadata_address,
            self.metadata_size,
            self.payload_size,
            self.tlv_size,
            self.signature_size,
            self.public_key_size,
            self.flags,
            0,
            self.firmware_digest,
            self.public_key_digest,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "ManifestHeader":
        if len(data) < HEADER_SIZE:
            raise ToolError("Metadata header 不完整")

        (
            magic,
            format_version,
            header_size,
            algorithm_id,
            image_start,
            image_end,
            metadata_address,
            metadata_size,
            payload_size,
            tlv_size,
            signature_size,
            public_key_size,
            flags,
            reserved,
            firmware_digest,
            public_key_digest,
        ) = struct.unpack(HEADER_FORMAT, data[:HEADER_SIZE])

        if magic != MAGIC:
            raise ToolError("Metadata magic 不正確")
        if format_version != FORMAT_VERSION:
            raise ToolError(f"Metadata version 不相容：{format_version}")
        if header_size != HEADER_SIZE:
            raise ToolError("Metadata header size 不正確")
        if algorithm_id not in ALG_NAMES:
            raise ToolError(f"未知 ML-DSA algorithm id：{algorithm_id}")
        if reserved != 0:
            raise ToolError("Metadata reserved 欄位不為 0")
        if payload_size != image_end - image_start:
            raise ToolError("Payload size 與 Image 範圍不一致")
        if HEADER_SIZE + tlv_size > metadata_size:
            raise ToolError("Metadata TLV 超出保留空間")

        return cls(
            algorithm_id=algorithm_id,
            image_start=image_start,
            image_end=image_end,
            metadata_address=metadata_address,
            metadata_size=metadata_size,
            payload_size=payload_size,
            tlv_size=tlv_size,
            signature_size=signature_size,
            public_key_size=public_key_size,
            flags=flags,
            firmware_digest=firmware_digest,
            public_key_digest=public_key_digest,
        )


def command_check(args: argparse.Namespace) -> None:
    info = check_openssl(args.openssl)
    print(json.dumps(info, ensure_ascii=False, indent=2))


def command_keygen(args: argparse.Namespace) -> None:
    info = check_openssl(args.openssl)
    openssl = info["executable"]

    private_key = Path(args.private_key)
    public_key = Path(args.public_key)
    private_key.parent.mkdir(parents=True, exist_ok=True)
    public_key.parent.mkdir(parents=True, exist_ok=True)

    if (private_key.exists() or public_key.exists()) and not args.force:
        raise ToolError("金鑰檔已存在。使用 --force 才可覆寫。")

    run_command([
        openssl,
        "genpkey",
        "-algorithm",
        args.algorithm,
        "-out",
        str(private_key),
    ])

    run_command([
        openssl,
        "pkey",
        "-in",
        str(private_key),
        "-pubout",
        "-out",
        str(public_key),
    ])

    print("Key generation OK")
    print(f"Algorithm : {args.algorithm}")
    print(f"Private   : {private_key}")
    print(f"Public    : {public_key}")


def build_unsigned_manifest(
    *,
    algorithm: str,
    image_start: int,
    image_end: int,
    metadata_address: int,
    metadata_size: int,
    firmware_digest: bytes,
    public_key: bytes,
    signature_size: int,
    build_info: bytes,
) -> tuple[ManifestHeader, bytes]:
    public_key_tlv = encode_tlv(TLV_PUBLIC_KEY, public_key)
    build_info_tlv = encode_tlv(TLV_BUILD_INFO, build_info) if build_info else b""
    signature_placeholder = encode_tlv(TLV_SIGNATURE, b"\x00" * signature_size)
    tlv_size = len(public_key_tlv) + len(build_info_tlv) + len(signature_placeholder)

    header = ManifestHeader(
        algorithm_id=ALG_IDS[algorithm],
        image_start=image_start,
        image_end=image_end,
        metadata_address=metadata_address,
        metadata_size=metadata_size,
        payload_size=image_end - image_start,
        tlv_size=tlv_size,
        signature_size=signature_size,
        public_key_size=len(public_key),
        flags=0,
        firmware_digest=firmware_digest,
        public_key_digest=hashlib.sha256(public_key).digest(),
    )

    signed_region = DOMAIN + header.pack() + public_key_tlv + build_info_tlv
    return header, signed_region


def command_sign(args: argparse.Namespace) -> None:
    info = check_openssl(args.openssl)
    openssl = info["executable"]

    input_hex = Path(args.input)
    output_hex = Path(args.output)
    private_key_path = Path(args.private_key)
    public_key_path = Path(args.public_key)

    if not input_hex.exists():
        raise ToolError(f"找不到 Input HEX：{input_hex}")
    if not private_key_path.exists():
        raise ToolError(f"找不到 Private Key：{private_key_path}")
    if not public_key_path.exists():
        raise ToolError(f"找不到 Public Key：{public_key_path}")

    image_start = parse_int(args.image_start)
    image_end = parse_int(args.image_end)
    metadata_address = parse_int(args.metadata_address)
    metadata_size = parse_int(args.metadata_size)

    memory, extra_records = parse_hex(input_hex)
    for address in range(metadata_address, metadata_address + metadata_size):
        memory.pop(address, None)

    firmware = get_image_bytes(
        memory,
        image_start,
        image_end,
        metadata_address,
        metadata_size,
    )
    firmware_digest = hashlib.sha256(firmware).digest()
    public_key = public_key_path.read_bytes()
    build_info = args.build_info.encode("utf-8") if args.build_info else b""

    # ML-DSA signatures are fixed-size per parameter set, but determine the exact
    # size using the active OpenSSL build before constructing the final header.
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        probe_message = temporary / "probe.bin"
        probe_signature = temporary / "probe.sig"
        probe_message.write_bytes(DOMAIN + b"probe")

        run_command([
            openssl,
            "pkeyutl",
            "-sign",
            "-rawin",
            "-in",
            str(probe_message),
            "-inkey",
            str(private_key_path),
            "-out",
            str(probe_signature),
        ])
        signature_size = probe_signature.stat().st_size

        header, signed_region = build_unsigned_manifest(
            algorithm=args.algorithm,
            image_start=image_start,
            image_end=image_end,
            metadata_address=metadata_address,
            metadata_size=metadata_size,
            firmware_digest=firmware_digest,
            public_key=public_key,
            signature_size=signature_size,
            build_info=build_info,
        )

        message_path = temporary / "manifest_to_sign.bin"
        signature_path = temporary / "manifest.sig"
        message_path.write_bytes(signed_region)

        run_command([
            openssl,
            "pkeyutl",
            "-sign",
            "-rawin",
            "-in",
            str(message_path),
            "-inkey",
            str(private_key_path),
            "-out",
            str(signature_path),
        ])
        signature = signature_path.read_bytes()

    if len(signature) != signature_size:
        raise ToolError(
            f"ML-DSA signature 長度改變：預期 {signature_size}，實際 {len(signature)}"
        )

    public_key_tlv = encode_tlv(TLV_PUBLIC_KEY, public_key)
    build_info_tlv = encode_tlv(TLV_BUILD_INFO, build_info) if build_info else b""
    signature_tlv = encode_tlv(TLV_SIGNATURE, signature)
    metadata = header.pack() + public_key_tlv + build_info_tlv + signature_tlv

    if len(metadata) > metadata_size:
        raise ToolError(
            f"Metadata 需要 {len(metadata)} bytes，但只保留 {metadata_size} bytes"
        )

    metadata += b"\xFF" * (metadata_size - len(metadata))
    for offset, value in enumerate(metadata):
        memory[metadata_address + offset] = value

    write_hex(output_hex, memory, extra_records)

    report = {
        "result": "SIGNED",
        "algorithm": args.algorithm,
        "input_hex": str(input_hex),
        "output_hex": str(output_hex),
        "image_start": f"0x{image_start:08X}",
        "image_end": f"0x{image_end:08X}",
        "metadata_address": f"0x{metadata_address:08X}",
        "metadata_size": metadata_size,
        "metadata_used": len(header.pack()) + len(public_key_tlv) + len(build_info_tlv) + len(signature_tlv),
        "firmware_sha256": firmware_digest.hex(),
        "public_key_sha256": hashlib.sha256(public_key).hexdigest(),
        "signature_size": len(signature),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


def command_verify(args: argparse.Namespace) -> None:
    info = check_openssl(args.openssl)
    openssl = info["executable"]

    input_hex = Path(args.input)
    metadata_address = parse_int(args.metadata_address)
    memory, _ = parse_hex(input_hex)

    header_bytes = bytes(
        memory.get(metadata_address + offset, 0xFF)
        for offset in range(HEADER_SIZE)
    )
    header = ManifestHeader.unpack(header_bytes)

    if header.metadata_address != metadata_address:
        raise ToolError(
            f"CLI Metadata Address 0x{metadata_address:08X} 與 Header "
            f"0x{header.metadata_address:08X} 不一致"
        )

    tlv_blob = bytes(
        memory.get(metadata_address + HEADER_SIZE + offset, 0xFF)
        for offset in range(header.tlv_size)
    )
    tlvs = decode_tlvs(tlv_blob)

    public_key = tlvs.get(TLV_PUBLIC_KEY)
    signature = tlvs.get(TLV_SIGNATURE)
    build_info = tlvs.get(TLV_BUILD_INFO, b"")

    if public_key is None:
        raise ToolError("Metadata 缺少 Public Key TLV")
    if signature is None:
        raise ToolError("Metadata 缺少 Signature TLV")
    if len(public_key) != header.public_key_size:
        raise ToolError("Public Key size mismatch")
    if len(signature) != header.signature_size:
        raise ToolError("Signature size mismatch")
    if hashlib.sha256(public_key).digest() != header.public_key_digest:
        raise ToolError("Embedded public key SHA256 mismatch")

    if args.trusted_public_key:
        trusted_key = Path(args.trusted_public_key).read_bytes()
        if trusted_key != public_key:
            raise ToolError("Embedded public key 與外部可信公鑰不同")

    firmware = get_image_bytes(
        memory,
        header.image_start,
        header.image_end,
        metadata_address,
        header.metadata_size,
    )
    firmware_digest = hashlib.sha256(firmware).digest()
    if firmware_digest != header.firmware_digest:
        raise ToolError("Firmware SHA256 mismatch")

    public_key_tlv = encode_tlv(TLV_PUBLIC_KEY, public_key)
    build_info_tlv = encode_tlv(TLV_BUILD_INFO, build_info) if build_info else b""
    signed_region = DOMAIN + header.pack() + public_key_tlv + build_info_tlv

    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        public_key_path = temporary / "public.pem"
        signature_path = temporary / "signature.bin"
        message_path = temporary / "manifest.bin"

        public_key_path.write_bytes(public_key)
        signature_path.write_bytes(signature)
        message_path.write_bytes(signed_region)

        run_command([
            openssl,
            "pkeyutl",
            "-verify",
            "-rawin",
            "-pubin",
            "-in",
            str(message_path),
            "-inkey",
            str(public_key_path),
            "-sigfile",
            str(signature_path),
        ])

    report = {
        "result": "VALID",
        "algorithm": ALG_NAMES[header.algorithm_id],
        "input_hex": str(input_hex),
        "firmware_sha256": firmware_digest.hex(),
        "public_key_sha256": hashlib.sha256(public_key).hexdigest(),
        "trusted_public_key": bool(args.trusted_public_key),
        "build_info": build_info.decode("utf-8", errors="replace"),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


def command_selftest(_args: argparse.Namespace) -> None:
    digest_a = hashlib.sha256(b"firmware").digest()
    digest_b = hashlib.sha256(b"public-key").digest()
    original = ManifestHeader(
        algorithm_id=65,
        image_start=0x08000000,
        image_end=0x0803E000,
        metadata_address=0x0803E000,
        metadata_size=0x2000,
        payload_size=0x3E000,
        tlv_size=4096,
        signature_size=3309,
        public_key_size=1952,
        flags=0,
        firmware_digest=digest_a,
        public_key_digest=digest_b,
    )
    packed = original.pack()
    restored = ManifestHeader.unpack(packed)
    if original != restored:
        raise ToolError("Manifest Header pack/unpack self-test failed")

    tlv_blob = encode_tlv(TLV_PUBLIC_KEY, b"pk") + encode_tlv(TLV_SIGNATURE, b"sig")
    decoded = decode_tlvs(tlv_blob)
    if decoded[TLV_PUBLIC_KEY] != b"pk" or decoded[TLV_SIGNATURE] != b"sig":
        raise ToolError("TLV encode/decode self-test failed")

    print(json.dumps({
        "result": "SELFTEST PASS",
        "header_size": HEADER_SIZE,
        "header_format": HEADER_FORMAT,
    }, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="OpenSSL ML-DSA Intel HEX signing tool V4"
    )
    parser.add_argument("--openssl")

    subparsers = parser.add_subparsers(required=True)

    check_parser = subparsers.add_parser("check")
    check_parser.set_defaults(func=command_check)

    selftest_parser = subparsers.add_parser("selftest")
    selftest_parser.set_defaults(func=command_selftest)

    key_parser = subparsers.add_parser("keygen")
    key_parser.add_argument("--algorithm", choices=ALG_IDS, default="ML-DSA-65")
    key_parser.add_argument("--private-key", required=True)
    key_parser.add_argument("--public-key", required=True)
    key_parser.add_argument("--force", action="store_true")
    key_parser.set_defaults(func=command_keygen)

    sign_parser = subparsers.add_parser("sign")
    sign_parser.add_argument("--algorithm", choices=ALG_IDS, default="ML-DSA-65")
    sign_parser.add_argument("--input", required=True)
    sign_parser.add_argument("--output", required=True)
    sign_parser.add_argument("--private-key", required=True)
    sign_parser.add_argument("--public-key", required=True)
    sign_parser.add_argument("--image-start", default="0x08000000")
    sign_parser.add_argument("--image-end", default="0x0803E000")
    sign_parser.add_argument("--metadata-address", default="0x0803E000")
    sign_parser.add_argument("--metadata-size", default="0x2000")
    sign_parser.add_argument("--build-info", default="")
    sign_parser.set_defaults(func=command_sign)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--input", required=True)
    verify_parser.add_argument("--metadata-address", default="0x0803E000")
    verify_parser.add_argument("--trusted-public-key")
    verify_parser.set_defaults(func=command_verify)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        args.func(args)
        return 0
    except (ToolError, OSError, ValueError, struct.error) as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
