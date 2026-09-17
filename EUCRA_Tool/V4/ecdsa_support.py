from __future__ import annotations
import subprocess
from pathlib import Path

ALGORITHM = 'ECDSA-P256-SHA256'
ALGORITHM_ID = 0x1001
SIGNATURE_SIZE = 64

def _read_len(data: bytes, off: int):
    first=data[off]; off+=1
    if first < 0x80: return first, off
    n=first & 0x7f
    if n == 0 or n > 4: raise ValueError('invalid DER length')
    return int.from_bytes(data[off:off+n], 'big'), off+n

def _enc_len(n: int):
    if n < 0x80: return bytes([n])
    b=n.to_bytes((n.bit_length()+7)//8,'big'); return bytes([0x80|len(b)])+b

def _integer(v: bytes):
    v=v.lstrip(b'\0') or b'\0'
    if v[0]&0x80: v=b'\0'+v
    return b'\x02'+_enc_len(len(v))+v

def der_to_raw(sig: bytes):
    if not sig or sig[0] != 0x30: raise ValueError('ECDSA signature is not DER')
    total,o=_read_len(sig,1)
    if o+total != len(sig): raise ValueError('bad DER sequence length')
    out=[]
    for _ in range(2):
        if sig[o] != 2: raise ValueError('missing DER integer')
        n,p=_read_len(sig,o+1); v=sig[p:p+n].lstrip(b'\0'); o=p+n
        if len(v)>32: raise ValueError('P-256 integer too long')
        out.append(v.rjust(32,b'\0'))
    return b''.join(out)

def raw_to_der(sig: bytes):
    if len(sig) != 64: raise ValueError('P-256 raw signature must be 64 bytes')
    body=_integer(sig[:32])+_integer(sig[32:])
    return b'\x30'+_enc_len(len(body))+body

def keygen(openssl: str, private_key: Path, public_key: Path):
    subprocess.run([openssl,'genpkey','-algorithm','EC','-pkeyopt','ec_paramgen_curve:P-256','-out',str(private_key)],check=True)
    subprocess.run([openssl,'pkey','-in',str(private_key),'-pubout','-out',str(public_key)],check=True)

def sign(openssl: str, private_key: Path, message: Path, output_raw: Path):
    der=output_raw.with_suffix('.der')
    subprocess.run([openssl,'dgst','-sha256','-sign',str(private_key),'-out',str(der),str(message)],check=True)
    output_raw.write_bytes(der_to_raw(der.read_bytes()))

def verify(openssl: str, public_key: Path, message: Path, signature_raw: Path):
    der=signature_raw.with_suffix('.der')
    der.write_bytes(raw_to_der(signature_raw.read_bytes()))
    subprocess.run([openssl,'dgst','-sha256','-verify',str(public_key),'-signature',str(der),str(message)],check=True)
