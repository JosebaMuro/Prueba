"""zipalign mínimo: reescribe el APK alineando los datos de las entradas sin comprimir.

Usa el mismo campo extra que el zipalign oficial de Android (id 0xD935), de modo que la
cabecera local es un ZIP estrictamente válido. Alinea a 4 bytes.
"""
import struct
import sys
import zipfile

ALIGN_ID = 0xD935


def align(src: str, dst: str, alignment: int = 4) -> None:
    with zipfile.ZipFile(src) as zin, open(dst, "wb") as raw:
        with zipfile.ZipFile(raw, "w") as zout:
            for info in zin.infolist():
                data = zin.read(info.filename)
                new = zipfile.ZipInfo(info.filename, date_time=(2024, 1, 1, 0, 0, 0))
                new.compress_type = info.compress_type
                new.external_attr = 0
                new.create_system = 0
                new.extra = b""
                if info.compress_type == zipfile.ZIP_STORED:
                    base = raw.tell() + 30 + len(info.filename.encode()) + 6  # 6 = cabecera del campo + uint16
                    pad = (-base) % alignment
                    new.extra = struct.pack("<HHH", ALIGN_ID, 2 + pad, alignment) + b"\x00" * pad
                zout.writestr(new, data)


if __name__ == "__main__":
    align(sys.argv[1], sys.argv[2])
