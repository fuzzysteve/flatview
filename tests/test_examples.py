"""Keep the README quick start honest: run it and compare with its comments."""

import re
import runpy
from pathlib import Path

EXAMPLE = Path(__file__).parent.parent / "examples" / "quickstart.py"


def test_quickstart_output_matches_its_comments(capsys):
    runpy.run_path(str(EXAMPLE), run_name="__main__")
    printed = capsys.readouterr().out.splitlines()
    src = EXAMPLE.read_text().splitlines()
    expected = []
    for i, line in enumerate(src):
        m = re.match(r"\s*print\(.*\)\s*#\s*(.*?)(\s{2,}.*)?$", line)
        if m:
            expected.append(m.group(1))
        elif line.strip().startswith("print(") and i + 1 < len(src) and src[i + 1].strip().startswith("# "):
            expected.append(src[i + 1].strip()[2:])
    assert printed == expected
