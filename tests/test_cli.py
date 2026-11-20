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


def _corrupt_categories(tmp_path):
    import shutil
    import struct
    import flatview
    data = bytearray(flatview.read_bytes(SDE / "categories.fb"))
    struct.pack_into("<I", data, 200, 0x7FFFFFF0)              # one row's offset now points far away
    (tmp_path / "bad.fb").write_bytes(data)
    shutil.copy(SDE / "categories.bfbs", tmp_path / "bad.bfbs")
    return str(tmp_path / "bad.fb")


@pytest.mark.parametrize("args", [["dump", "--jsonl"], ["info"], ["get", "45"]])
def test_reading_commands_verify_first(tmp_path, capsys, args):
    bad = _corrupt_categories(tmp_path)
    assert main([args[0], bad, *args[1:]]) == 1
    out, err = capsys.readouterr()
    assert out == "" and "Traceback" not in err
    assert "is not a valid FlatBuffer for this schema: rows[44]: offset to table points outside" in err
    assert "--no-verify" in err


def test_no_verify_reads_until_it_fails_cleanly(tmp_path, capsys):
    bad = _corrupt_categories(tmp_path)
    assert main(["dump", bad, "--jsonl", "--no-verify"]) == 1
    out, err = capsys.readouterr()
    assert len(out.splitlines()) == 44                          # the rows before the damage
    assert err.startswith("error: cannot read") and "flatview verify" in err
    assert main(["dump", bad, "--jsonl", "--limit", "3", "--no-verify"]) == 0


def _unsorted(tmp_path):
    import flatview
    (tmp_path / "u.fbs").write_text("table R { k: int (key); } table T { rs: [R]; } root_type T;")
    s = flatview.load_schema(tmp_path / "u.fbs")
    (tmp_path / "u.bin").write_bytes(s.encode({"rs": [{"k": 2}, {"k": 1}]}, sort_keys=False))
    return [str(tmp_path / "u.bin"), "-s", str(tmp_path / "u.fbs")]


def test_unsorted_keys_are_data_for_info_and_dump(tmp_path, capsys):
    f = _unsorted(tmp_path)
    assert main(["info", *f]) == 0
    assert "sorted: False" in capsys.readouterr().out
    assert main(["dump", *f, "--jsonl"]) == 0
    assert capsys.readouterr().out.splitlines() == ['{"k": 2}', '{"k": 1}']
    # get binary-searches, so it still needs the order
    assert main(["get", *f, "1"]) == 1
    assert "not sorted" in capsys.readouterr().err


@pytest.mark.parametrize("args, msg", [
    (["dump", "--path", "nosuch"], "--path nosuch: the root has no 'nosuch'"),
    (["dump", "--path", "rs.9"], "--path rs.9: rs has no '9'"),
    (["dump", "--path", "rs.x"], "--path rs.x: rs has no 'x'"),
    (["dump", "--path", "rs.0.k.z"], "--path rs.0.k.z: rs.0.k has no 'z'"),
    (["get", "abc"], "key 'abc': k is a number"),
])
def test_bad_input_is_a_usage_error(tmp_path, capsys, args, msg):
    f = _unsorted(tmp_path)
    assert main([args[0], *f, *args[1:]] if args[0] == "dump" else ["get", *f, "--no-verify", *args[1:]]) == 2
    err = capsys.readouterr().err
    assert err == f"error: {msg}\n"


@pytest.mark.parametrize("payload", [b"\x1f\x8b\x08\x00" + b"\xff" * 40, b"\x1f\x8b"])
def test_corrupt_gzip_is_one_line(tmp_path, capsys, payload):
    f = _unsorted(tmp_path)
    (tmp_path / "u.bin").write_bytes(payload)
    assert main(["info", *f]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ") and err.count("\n") == 1 and "gzip" in err


def test_internal_errors_outside_reading_keep_their_traceback(tmp_path, monkeypatch):
    import flatview
    f = _unsorted(tmp_path)
    (tmp_path / "v.json").write_text("{}")

    def boom(*a, **k):
        raise IndexError("bug")
    monkeypatch.setattr(flatview.Schema, "encode", boom)
    with pytest.raises(IndexError):
        main(["encode", f[2], str(tmp_path / "v.json"), "-o", str(tmp_path / "o.bin")])
