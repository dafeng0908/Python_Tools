# PQC Firmware Signing Tool V4

V4 修正 V3 的 `struct.pack expected 11 items` 問題，並重新設計 Metadata 格式。

## V4 主要變更

- 使用 `ManifestHeader` dataclass 管理固定 Header。
- Header `pack()` / `unpack()` 共用同一份格式定義。
- 新增 `selftest`，可在沒有 OpenSSL 的情況下檢查 Header 與 TLV。
- Metadata 改成 TLV：Public Key / Build Info / Signature。
- OpenSSL 自動偵測，使用 `pkeyutl -rawin`。
- KeyGen 預設禁止覆寫既有金鑰。
- CLI 輸出 JSON，方便 Jenkins 解析。
- GUI 自動管理 project/keys、project/output、project/log。
- Sign 與 Verify 不需要使用者選擇 Output。

## 安裝與啟動

```powershell
python -m pip install -r requirements.txt
python pqc_openssl_gui_v4.py
```

## Self-Test

```powershell
python pqc_openssl_hex_v4.py selftest
```

## Metadata

```text
ManifestHeader
Public Key TLV
Build Info TLV
Signature TLV
0xFF padding
```

簽章涵蓋 `DOMAIN + ManifestHeader + Public Key TLV + Build Info TLV`；Firmware 本體透過 Header 內 SHA-256 digest 被簽章保護。

## Security

正式產品不可只信任 Signed HEX 內嵌的 Public Key。MCU 必須在受保護區域保存可信公鑰、公鑰 SHA-256，或另一個可驗證此公鑰的 Root Key。

## V4.0.1

- 修正 Manifest Header 格式欄位數。
- Header 固定大小為 120 bytes。
- 已完成 pack/unpack round-trip 與 TLV 測試。
