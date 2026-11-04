"""检查 SPK 里是否带上了某个修复（打包后的确认用）。"""
import gzip
import io
import sys
import tarfile

spk = sys.argv[1]
checks = sys.argv[2:]

with tarfile.open(spk) as t:
    inner = [m for m in t.getmembers() if m.name.endswith("package.tgz")][0]
    data = t.extractfile(inner).read()

with gzip.open(io.BytesIO(data)) as g:
    with tarfile.open(fileobj=g) as pkg:
        names = pkg.getnames()
        files = {}
        for suffix in ("server.py", "app.js", "index.html", "logs.html",
                       "syncer.py", "pc189.py", "cloud.py", "style.css"):
            hit = [n for n in names if n.endswith(suffix)]
            if hit:
                files[suffix] = pkg.extractfile(hit[0]).read().decode("utf-8")

ok = True
for item in checks:
    key, _, needle = item.partition("=")
    key = key.strip()
    if key not in files:
        print("  [缺文件] %s" % key)
        ok = False
        continue
    found = needle in files[key]
    print("  [%s] %s" % ("有" if found else "无", item))
    if not found:
        ok = False

sys.exit(0 if ok else 1)
