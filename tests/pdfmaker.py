"""Builds tiny text PDFs for tests (one line per entry; lines use Tj, the last one uses a TJ array with a word gap)."""
import zlib


def make_pdf(lines, compress=True):
    ops = ["BT", "/F1 12 Tf", "72 720 Td", "14 TL"]
    for n, line in enumerate(lines):
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if n == len(lines) - 1 and " " in line:
            head, tail = safe.split(" ", 1)
            ops.append("[(%s) -300 (%s)] TJ" % (head, tail))
        else:
            ops.append("(%s) Tj" % safe)
        ops.append("T*")
    ops.append("ET")
    content = "\n".join(ops).encode("latin-1")
    stream = zlib.compress(content) if compress else content
    flt = b"/Filter /FlateDecode " if compress else b""
    return (b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>\nendobj\n"
            b"4 0 obj\n<< " + flt + b"/Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream +
            b"\nendstream\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n")
