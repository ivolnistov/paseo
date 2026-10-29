"""Compare APK payload entries, ignoring signature metadata. Prints only names/booleans; exits 1 on mismatch."""
import re
import sys
import zipfile

SIG = re.compile(r"^META-INF/([^/]+\.(SF|RSA|DSA|EC)|MANIFEST\.MF|com/android/(otacerts|metadata))$|^stamp-cert-sha256$")


def entries(path):
    with zipfile.ZipFile(path) as z:
        return {i.filename: (i.CRC, i.file_size, i.compress_type, z.read(i.filename)) for i in z.infolist() if not SIG.match(i.filename)}


a, b = entries(sys.argv[1]), entries(sys.argv[2])
only_a = sorted(set(a) - set(b))
only_b = sorted(set(b) - set(a))
diff = sorted(n for n in set(a) & set(b) if a[n] != b[n])
identical = bool(a) and not only_a and not only_b and not diff
print(f"entries unsigned={len(a)} signed={len(b)}")
print(f"only_in_unsigned={only_a[:10]} only_in_signed={only_b[:10]} content_or_method_diff={diff[:10]}")
print(f"payload_identical={identical}")
sys.exit(0 if identical else 1)
