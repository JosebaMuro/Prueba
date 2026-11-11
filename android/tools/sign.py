"""Firma un APK con los esquemas v1 (JAR) y v2, sin depender del SDK de Android.

Uso: sign.py <keystore.p12> <contraseña> <entrada.apk> <salida.apk>

1. v1: añade META-INF/MANIFEST.MF, CERT.SF y CERT.RSA (PKCS#7) como hace jarsigner/apksigner.
2. Alinea a 4 bytes las entradas sin comprimir (campo extra 0xD935, como zipalign).
3. v2: inserta el "APK Signing Block" antes del directorio central (RSA PKCS#1 v1.5 + SHA-256).
"""
import base64
import hashlib
import struct
import sys
import zipfile

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import pkcs7, pkcs12

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from zipalign import align  # noqa: E402

CREATED_BY = "1.0 (Studio IA Local)"


def _b64sha(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def _wrap(line: str) -> str:
    """Las líneas de un manifiesto JAR no pueden superar 72 bytes: se continúan con un espacio."""
    raw = line.encode()
    out, first = [], True
    while raw:
        n = 70 if first else 69
        out.append(("" if first else " ") + raw[:n].decode("utf-8", "ignore"))
        raw, first = raw[n:], False
    return "\r\n".join(out) + "\r\n"


def sign_v1(src: str, dst: str, key, cert) -> None:
    with zipfile.ZipFile(src) as zin:
        names = [n for n in zin.namelist() if not n.startswith("META-INF/")]
        main = "Manifest-Version: 1.0\r\nCreated-By: %s\r\n\r\n" % CREATED_BY
        sections = {n: _wrap("Name: " + n) + "SHA-256-Digest: %s\r\n\r\n" % _b64sha(zin.read(n)) for n in names}
        manifest = (main + "".join(sections[n] for n in names)).encode()
        sf = "Signature-Version: 1.0\r\nCreated-By: %s\r\nSHA-256-Digest-Manifest: %s\r\nX-Android-APK-Signed: 2\r\n\r\n" % (
            CREATED_BY, _b64sha(manifest))
        sf += "".join(_wrap("Name: " + n) + "SHA-256-Digest: %s\r\n\r\n" % _b64sha(sections[n].encode()) for n in names)
        sf = sf.encode()
        rsa = pkcs7.PKCS7SignatureBuilder().set_data(sf).add_signer(cert, key, hashes.SHA256()).sign(
            serialization.Encoding.DER,
            [pkcs7.PKCS7Options.DetachedSignature, pkcs7.PKCS7Options.Binary, pkcs7.PKCS7Options.NoAttributes])
        with zipfile.ZipFile(dst, "w") as zout:
            for name, data in (("META-INF/MANIFEST.MF", manifest), ("META-INF/CERT.SF", sf), ("META-INF/CERT.RSA", rsa)):
                zout.writestr(zipfile.ZipInfo(name, date_time=(2024, 1, 1, 0, 0, 0)), data, zipfile.ZIP_DEFLATED)
            for n in names:
                info = zin.getinfo(n)
                zout.writestr(zipfile.ZipInfo(n, date_time=(2024, 1, 1, 0, 0, 0)), zin.read(n), info.compress_type)


def _lp(b: bytes) -> bytes:
    return struct.pack("<I", len(b)) + b


def _chunked_digest(parts: list[bytes]) -> bytes:
    digests = []
    for part in parts:
        for i in range(0, len(part), 1 << 20):
            chunk = part[i:i + (1 << 20)]
            digests.append(hashlib.sha256(b"\xa5" + struct.pack("<I", len(chunk)) + chunk).digest())
    return hashlib.sha256(b"\x5a" + struct.pack("<I", len(digests)) + b"".join(digests)).digest()


def sign_v2(src: str, dst: str, key, cert) -> None:
    apk = open(src, "rb").read()
    eocd_off = apk.rfind(b"PK\x05\x06")
    assert eocd_off >= 0 and eocd_off + 22 == len(apk), "EOCD inesperado (¿comentario ZIP?)"
    cd_size, cd_off = struct.unpack("<II", apk[eocd_off + 12:eocd_off + 20])
    assert cd_off + cd_size == eocd_off
    entries, cd, eocd = apk[:cd_off], apk[cd_off:eocd_off], apk[eocd_off:]

    alg = 0x0103  # RSASSA-PKCS1-v1_5 con SHA2-256
    digest = _chunked_digest([entries, cd, eocd])  # el EOCD se digiere con el offset original del directorio central
    signed_data = (_lp(_lp(struct.pack("<I", alg) + _lp(digest)))
                   + _lp(_lp(cert.public_bytes(serialization.Encoding.DER)))
                   + _lp(b""))
    signature = key.sign(signed_data, padding.PKCS1v15(), hashes.SHA256())
    pub = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    signer = _lp(signed_data) + _lp(_lp(struct.pack("<I", alg) + _lp(signature))) + _lp(pub)
    value = _lp(_lp(signer))
    pair = struct.pack("<QI", 4 + len(value), 0x7109871A) + value
    size = len(pair) + 8 + 16
    block = struct.pack("<Q", size) + pair + struct.pack("<Q", size) + b"APK Sig Block 42"
    new_eocd = eocd[:16] + struct.pack("<I", cd_off + len(block)) + eocd[20:]
    open(dst, "wb").write(entries + block + cd + new_eocd)


def main(ks: str, password: str, src: str, dst: str) -> None:
    key, cert, _ = pkcs12.load_key_and_certificates(open(ks, "rb").read(), password.encode())
    sign_v1(src, dst + ".v1", key, cert)
    align(dst + ".v1", dst + ".aligned")
    sign_v2(dst + ".aligned", dst, key, cert)


if __name__ == "__main__":
    main(*sys.argv[1:5])
