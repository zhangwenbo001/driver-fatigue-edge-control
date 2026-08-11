"""G3-01B delivery lint check."""
import py_compile, sys, os
base = os.path.dirname(os.path.abspath(__file__))
files = ["ui.py", "serial_bridge.py", "config.py", "main.py"]
ok = 0
for f in files:
    try:
        py_compile.compile(os.path.join(base, f), doraise=True)
        ok += 1
    except py_compile.PyCompileError as e:
        print(f"FAIL: {f} - {e}")
        sys.exit(1)
print(f"LINT OK: {ok}/{len(files)}")
