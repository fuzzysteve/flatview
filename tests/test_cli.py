import json

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
