import json
import math

import pytest

from flatview.__main__ import main

from conftest import SDE


def test_get(capsys):
    assert main(["get", str(SDE / "categories.fb"), "6"]) == 0
    assert json.loads(capsys.readouterr().out)["name"]["en"] == "Ship"


def test_get_missing(capsys):
    assert main(["get", str(SDE / "categories.fb"), "123456"]) == 1


def test_dump_jsonl(capsys):
    assert main(["dump", str(SDE / "compressibleTypes.fb"), "--jsonl", "--limit", "3"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(x) for x in lines][0] == {"typeID": 18, "compressedTypeID": 62528}
    assert len(lines) == 3


def test_verify_and_info(capsys):
    assert main(["verify", str(SDE / "dogmaEffects.fb"), "--strict-enums"]) == 0
    assert capsys.readouterr().out.startswith("OK")
    assert main(["info", str(SDE / "masteries.fb")]) == 0
    assert "key: typeID, sorted: True" in capsys.readouterr().out


def test_schema_and_encode(tmp_path, capsys):
    assert main(["schema", str(SDE / "compressibleTypes.bfbs")]) == 0
    fbs = tmp_path / "ct.fbs"
    fbs.write_text(capsys.readouterr().out)
    js = tmp_path / "in.json"
    js.write_text(json.dumps({"rows": [{"typeID": 20, "compressedTypeID": 1}, {"typeID": 3, "compressedTypeID": 2}]}))
    out = tmp_path / "ct.fb"
    assert main(["encode", str(fbs), str(js), "-o", str(out)]) == 0
    capsys.readouterr()
    assert main(["get", str(out), "20"]) == 0
    assert json.loads(capsys.readouterr().out)["compressedTypeID"] == 1


# --- untrusted strings must not reach the terminal raw ----------------------

import re
import sys

import flatview

HOSTILE = "a\x1b[2Jb\x9b31mc\u202edcba\u2028e\x7ff\u2066g"
RAW_UNSAFE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]")


@pytest.fixture
def hostile_files(tmp_path):
    schema = tmp_path / "h.fbs"
    schema.write_text(
        'attribute "note";\n'
        f"/// doc {HOSTILE}\n"
        'table T (note: "x\\u009by\\u202ez") { name: string; tags: [string]; }\n'
        'root_type T;\nfile_identifier "\\u009bAB";\n', encoding="utf-8")
    s = flatview.load_schema(schema)
    data = tmp_path / "h.bin"
    data.write_bytes(s.encode({"name": HOSTILE, "tags": [HOSTILE, "plain"]}))
    return schema, data


@pytest.mark.parametrize("args", [["dump"], ["dump", "--jsonl", "--path", "tags"]])
def test_dump_escapes_terminal_controls_losslessly(hostile_files, capsys, args):
    schema, data = hostile_files
    assert main([args[0], str(data), "-s", str(schema), *args[1:]]) == 0
    out = capsys.readouterr().out
    assert not RAW_UNSAFE.search(out)
    if "--jsonl" in args:
        assert [json.loads(line) for line in out.splitlines()] == [HOSTILE, "plain"]
    else:
        assert json.loads(out) == {"name": HOSTILE, "tags": [HOSTILE, "plain"]}


def test_info_escapes_identifier(hostile_files, capsys):
    schema, data = hostile_files
    assert main(["info", str(data), "-s", str(schema)]) == 0
    out = capsys.readouterr().out
    assert not RAW_UNSAFE.search(out) and "ident:   \\u009bAB" in out


@pytest.mark.parametrize("tty", [True, False])
def test_schema_output(hostile_files, capsys, monkeypatch, tty):
    schema, _ = hostile_files
    monkeypatch.setattr(sys.stdout, "isatty", lambda: tty)
    assert main(["schema", str(schema)]) == 0
    out = capsys.readouterr().out
    assert 'note: "x\\u009by\\u202ez"' in out            # strings: always escaped, losslessly
    if tty:
        assert not RAW_UNSAFE.search(out)                # doc comment neutralised on a terminal
    else:
        first, second = HOSTILE.split("\u2028")          # line breaks split doc comments
        assert f"/// doc {first}\n///{second}\n" in out   # redirected: otherwise exact
        again = flatview.parse_fbs(out)
        assert again["T"].documentation == [f" doc {HOSTILE.split(chr(0x2028))[0]}",
                                           HOSTILE.split(chr(0x2028))[1]]


def test_dump_output_encodes_again(tmp_path, capsys):
    # dump writes non-finite floats as "nan"/"inf" strings (JSON has none); encode must take them
    import flatview
    (tmp_path / "s.fbs").write_text("table T { a: double; b: float; c: [double]; } root_type T;")
    s = flatview.load_schema(tmp_path / "s.fbs")
    (tmp_path / "x.bin").write_bytes(s.encode({"a": float("nan"), "b": float("-inf"), "c": [float("inf"), 1.5]}))
    assert main(["dump", str(tmp_path / "x.bin"), "-s", str(tmp_path / "s.fbs")]) == 0
    (tmp_path / "x.json").write_text(capsys.readouterr().out)
    assert main(["encode", str(tmp_path / "s.fbs"), str(tmp_path / "x.json"), "-o", str(tmp_path / "y.bin")]) == 0
    again = s.read((tmp_path / "y.bin").read_bytes())
    assert math.isnan(again.a) and again.b == float("-inf") and again.c.tolist() == [float("inf"), 1.5]
