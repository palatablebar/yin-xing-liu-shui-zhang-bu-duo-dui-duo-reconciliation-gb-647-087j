"""补传合并：整段重出 vs 只补新增、同日同额同摘要、幂等与失败保旧。"""
import re

HEADER = "流水号,账户,发生时间,借贷标志,金额,手续费,对方户名,摘要\n"


def bank_csv(rows: list[tuple]) -> bytes:
    lines = [HEADER]
    for txn, day, amount, memo, direction in rows:
        lines.append(
            f"{txn},6222-0012,{day}T09:00:00+08:00,{direction},{amount},0.00,供应商,{memo}\n"
        )
    return "".join(lines).encode("utf-8")


BASE_ROWS = [
    ("B001", "2026-03-02", "100.00", "货款", "借"),
    ("B002", "2026-03-03", "200.00", "货款", "借"),
    ("B003", "2026-03-04", "300.00", "货款", "借"),
    ("B004", "2026-03-05", "400.00", "货款", "借"),
    ("B005", "2026-03-06", "500.00", "货款", "借"),
    # 同一天、同额、同摘要的两笔扣款：流水号不同，必须各自保留。
    ("B006", "2026-03-07", "66.00", "手续费", "借"),
    ("B007", "2026-03-07", "66.00", "手续费", "借"),
]


def upload(client, side, name, raw, mode=None):
    data = {"side": side}
    if mode:
        data["mode"] = mode
    response = client.post(
        "/api/statements/import",
        data=data,
        files={"file": (name, raw, "text/csv")},
    )
    return response


def rows(client, side="bank", status="valid"):
    return client.get("/api/statements", params={"side": side, "status": status}).json()["rows"]


def txn_map(client, side="bank", status="valid"):
    return {row["txn_no"]: row for row in rows(client, side, status)}


def test_whole_segment_reissue_replaces_dropped_and_corrected_rows(client):
    base = upload(client, "bank", "base.csv", bank_csv(BASE_ROWS))
    assert base.status_code == 200
    assert base.json()["merge_mode"] == "initial"

    reissue_rows = [
        ("B001", "2026-03-02", "110.00", "货款", "借"),   # 同流水号更正金额
        ("B002", "2026-03-03", "200.00", "货款", "借"),
        ("B003", "2026-03-04", "300.00", "货款", "借"),
        ("B005", "2026-03-06", "500.00", "货款", "借"),
        ("B006", "2026-03-07", "66.00", "手续费", "借"),
        ("B007", "2026-03-07", "66.00", "手续费", "借"),
        ("B008", "2026-03-07", "70.00", "补录", "借"),
    ]
    body = upload(client, "bank", "reissue.csv", bank_csv(reissue_rows)).json()
    assert body["merge_mode"] == "replace"
    assert body["merge"]["dropped_rows"] == 1
    mapping = txn_map(client)
    assert set(mapping) == {"B001", "B002", "B003", "B005", "B006", "B007", "B008"}
    assert mapping["B001"]["amount"] == "110.00"
    assert body["counts"]["bank_valid"] == 7


def test_append_only_keeps_old_unique_rows_even_when_ranges_overlap(client):
    upload(client, "bank", "base.csv", bank_csv(BASE_ROWS))
    append_rows = [
        # 日期落在旧文件区间之内，但流水号是新的：属于补新增，不是重出。
        ("B010", "2026-03-05", "400.00", "货款", "借"),
        ("B011", "2026-03-12", "800.00", "货款", "借"),
    ]
    body = upload(client, "bank", "late.csv", bank_csv(append_rows)).json()
    assert body["merge_mode"] == "cumulative"
    mapping = txn_map(client)
    assert set(mapping) == {row[0] for row in BASE_ROWS} | {"B010", "B011"}
    assert body["counts"]["bank_valid"] == 9


def test_reissue_does_not_swallow_late_deduction_from_another_file(client):
    upload(client, "bank", "base.csv", bank_csv(BASE_ROWS))
    # 晚到扣款：日期落进基准文件的区间，但来自另一份独立补传，与基准文件没有共同流水号。
    upload(client, "bank", "late.csv", bank_csv([("B010", "2026-03-03", "999.00", "晚到", "借")]))
    reissue = bank_csv([
        ("B001", "2026-03-02", "100.00", "货款", "借"),
        ("B002", "2026-03-03", "200.00", "货款", "借"),
        ("B003", "2026-03-04", "300.00", "货款", "借"),
        ("B005", "2026-03-06", "500.00", "货款", "借"),
        ("B006", "2026-03-07", "66.00", "手续费", "借"),
        ("B007", "2026-03-07", "66.00", "手续费", "借"),
    ])
    body = upload(client, "bank", "reissue.csv", reissue).json()
    assert body["merge_mode"] == "replace"
    mapping = txn_map(client)
    assert "B004" not in mapping  # 基准段内被撤回的行确实消失
    assert mapping["B010"]["amount"] == "999.00"  # 独立补传的晚到扣款保留


def test_same_day_same_amount_same_memo_not_collapsed(client):
    upload(client, "bank", "base.csv", bank_csv(BASE_ROWS))
    mapping = txn_map(client)
    assert mapping["B006"]["amount"] == "66.00"
    assert mapping["B007"]["amount"] == "66.00"
    assert mapping["B006"]["posted_at"] == mapping["B007"]["posted_at"]
    assert len([key for key in mapping if key in ("B006", "B007")]) == 2


def test_reimport_same_file_is_idempotent(client):
    first = upload(client, "bank", "base.csv", bank_csv(BASE_ROWS)).json()
    second = upload(client, "bank", "base.csv", bank_csv(BASE_ROWS)).json()
    assert first["run_id"] == second["run_id"]
    assert first["counts"] == second["counts"]
    assert first["totals"] == second["totals"]
    assert len(rows(client)) == 7


def test_same_file_group_replayed_in_any_order_stays_stable(client):
    base = bank_csv(BASE_ROWS)
    reissue = bank_csv([
        ("B001", "2026-03-02", "110.00", "货款", "借"),
        ("B002", "2026-03-03", "200.00", "货款", "借"),
        ("B003", "2026-03-04", "300.00", "货款", "借"),
        ("B005", "2026-03-06", "500.00", "货款", "借"),
        ("B006", "2026-03-07", "66.00", "手续费", "借"),
        ("B007", "2026-03-07", "66.00", "手续费", "借"),
    ])
    upload(client, "bank", "base.csv", base)
    upload(client, "bank", "reissue.csv", reissue)
    first = client.get("/api/differences").json()
    count = len(rows(client))

    upload(client, "bank", "base.csv", base)
    upload(client, "bank", "reissue.csv", reissue)
    second = client.get("/api/differences").json()

    assert len(rows(client)) == count
    assert first["counts"] == second["counts"]
    assert first["totals"] == second["totals"]


def test_failed_import_keeps_previous_version(client, demo):
    before = client.get("/api/differences").json()

    missing = upload(client, "bank", "missing.csv", b"")
    assert missing.status_code == 200  # 空文件不是异常，但也不能清空旧版

    broken = upload(client, "bank", "broken.csv", b"no header here\n1,2,3\n")
    assert broken.status_code == 200
    assert broken.json()["state"] == "HEADER_MISSING"

    not_found = client.post("/api/statements/import",
                            data={"side": "bank", "sample": "missing.csv"})
    assert not_found.status_code == 404

    after = client.get("/api/differences").json()
    assert after["run_id"] == before["run_id"]
    assert after["counts"] == before["counts"]
    assert after["totals"] == before["totals"]


def test_empty_upload_does_not_replace_existing_side(client):
    upload(client, "bank", "base.csv", bank_csv(BASE_ROWS))
    body = upload(client, "bank", "empty.csv", b"").json()
    assert body["state"] == "EMPTY_FILE"
    assert body["merge_mode"] == "noop"
    assert body["counts"]["bank_valid"] == 7
    assert len(rows(client)) == 7


def test_other_side_preserved_across_supplements(client):
    ledger = (
        "凭证号,账户,记账日期,方向,金额,摘要\n"
        "V001,6222-0012,2026-03-02,借,100.00,货款\n"
    ).encode("gb18030")
    upload(client, "ledger", "ledger.csv", ledger)
    upload(client, "bank", "base.csv", bank_csv(BASE_ROWS))
    upload(client, "bank", "late.csv", bank_csv([("B010", "2026-03-05", "400.00", "货款", "借")]))
    assert len(rows(client, "ledger")) == 1
    assert len(rows(client, "bank")) == 8


def test_forced_modes_override_auto_detection(client):
    upload(client, "bank", "base.csv", bank_csv(BASE_ROWS))
    # 覆盖整段但少 B004；自动判定会当成重出，强制累计应保留旧文件独有的 B004。
    whole = bank_csv([
        ("B001", "2026-03-02", "100.00", "货款", "借"),
        ("B002", "2026-03-03", "200.00", "货款", "借"),
        ("B003", "2026-03-04", "300.00", "货款", "借"),
        ("B005", "2026-03-06", "500.00", "货款", "借"),
        ("B006", "2026-03-07", "66.00", "手续费", "借"),
        ("B007", "2026-03-07", "66.00", "手续费", "借"),
    ])
    forced = upload(client, "bank", "whole.csv", whole, mode="cumulative").json()
    assert forced["merge_mode"] == "cumulative"
    assert len(rows(client)) == 7

    again = upload(client, "bank", "whole.csv", whole, mode="replace").json()
    assert again["merge_mode"] == "replace"
    assert {row["txn_no"] for row in rows(client)} == {
        "B001", "B002", "B003", "B005", "B006", "B007"
    }


def test_pages_and_exports_use_the_merged_set(client):
    upload(client, "bank", "base.csv", bank_csv(BASE_ROWS))
    upload(client, "ledger", "ledger.csv", b"")
    upload(client, "bank", "late.csv", bank_csv([("B010", "2026-03-05", "400.00", "货款", "借")]))

    summary = client.get("/api/export/summary").json()
    assert summary["counts"]["bank_valid"] == 8

    index = client.get("/")
    assert index.status_code == 200
    assert re.search(r"银行</b><em>8</em>笔", index.text)

    report = client.get("/api/export/report.csv").text
    unmatched = [line for line in report.splitlines()
                 if line.startswith("未匹配,") and ",银行," in line]
    assert len(unmatched) == 8  # 页面与下载文件引用同一批 8 笔有效交易
    assert "重复" not in report  # 重出副本没有被重复计入
    manifest = client.get("/api/export/manifest.json").json()
    assert manifest["counts"]["bank_valid"] == 8
    assert len(manifest["inputs"]["bank"]["effective_sha256"]) == 64
    sources = manifest["inputs"]["bank"]["sources"]
    assert [item["file"] for item in sources] == ["base.csv", "late.csv"]
    assert sources[0]["active"] is True and sources[1]["active"] is True

    batches = client.get("/api/batches").json()
    assert batches["batches"][0]["manifest"]["inputs"]["bank"]["merge"]["mode"] == "cumulative"


def test_piecewise_import_matches_single_full_import(settings):
    """同一批交易：一次全量运行 与 逐侧导入（各一次）必须得到同一 run_id 与合计。"""
    from pathlib import Path

    from fastapi.testclient import TestClient

    from app.main import create_app

    sample = Path(__file__).resolve().parents[1] / "sample_data"
    bank_raw = (sample / "bank_statement_dirty.csv").read_bytes()
    ledger_raw = (sample / "ledger_book_dirty.csv").read_bytes()

    demo_client = TestClient(create_app(settings))
    demo = demo_client.post("/api/demo/run").json()

    piece_client = TestClient(create_app(settings))
    piece_client.post("/api/statements/import", data={"side": "bank", "sample": "bank_statement_dirty.csv"})
    ledger = piece_client.post("/api/statements/import",
                               data={"side": "ledger", "sample": "ledger_book_dirty.csv"}).json()

    assert ledger["run_id"] == demo["run_id"]
    assert ledger["counts"] == demo["counts"]
    assert ledger["totals"] == demo["totals"]

    # 同一组文件再导一遍：笔数、合计、run_id 都不变。
    repeat = piece_client.post("/api/statements/import",
                               data={"side": "bank", "sample": "bank_statement_dirty.csv"}).json()
    assert repeat["run_id"] == demo["run_id"]
    assert repeat["counts"] == demo["counts"]
    assert repeat["totals"] == demo["totals"]
    assert ledger_raw and bank_raw  # 样例确实被读取


def test_reissue_invalidates_source_but_keeps_history(client):
    upload(client, "bank", "base.csv", bank_csv(BASE_ROWS))
    reissue = bank_csv([
        ("B001", "2026-03-02", "100.00", "货款", "借"),
        ("B002", "2026-03-03", "200.00", "货款", "借"),
        ("B003", "2026-03-04", "300.00", "货款", "借"),
        ("B005", "2026-03-06", "500.00", "货款", "借"),
        ("B006", "2026-03-07", "66.00", "手续费", "借"),
        ("B007", "2026-03-07", "66.00", "手续费", "借"),
    ])
    upload(client, "bank", "reissue.csv", reissue)
    manifest = client.get("/api/export/manifest.json").json()
    sources = {item["file"]: item for item in manifest["inputs"]["bank"]["sources"]}
    assert sources["base.csv"]["active"] is False
    # 重出文件重述了存活流水号并撤回了 B004；旧文件在有效集合里不再占行。
    assert sources["base.csv"]["rows_in_set"] == 0
    assert sources["reissue.csv"]["rows_in_set"] == 6
    assert sources["reissue.csv"]["active"] is True


def test_truncated_reissue_does_not_drop_old_rows(client):
    upload(client, "bank", "base.csv", bank_csv(BASE_ROWS))
    header = HEADER
    truncated = (
        header
        + "B001,6222-0012,2026-03-02T09:00:00+08:00,借,100.00,0.00,供应商,货款\n"
        + "B002,6222-0012,2026-03-03T09:00:00+08:00,借,200.00"  # 无换行且缺列
    ).encode("utf-8")
    body = upload(client, "bank", "trunc.csv", truncated).json()
    assert body["merge_mode"] == "cumulative"
    assert body["merge"]["structural_guard"] == "TRUNCATED_TAIL"
    assert len(rows(client)) == 6  # 旧文件独有的 B003..B005 没有被撤回


def test_reissue_of_dirty_file_does_not_stack_quarantine_evidence(client):
    dirty = (
        "流水号,账户,发生时间,借贷标志,金额,手续费,对方户名,摘要\n"
        "B001,6222-0012,2026-03-02T09:00:00+08:00,借,100.00,0.00,供应商,货款\n"
        "B002,6222-0012,2026-03-03T09:00:00+08:00,借,N/A,0.00,供应商,空值\n"
        "B003,6222-0012,2026-03-04T09:00:00+08:00,X,100.00,0.00,供应商,符号错\n"
    ).encode("utf-8")
    first = upload(client, "bank", "dirty.csv", dirty).json()
    assert first["quarantine"] == 2
    assert len(rows(client, "bank", "quarantine")) == 2

    second = upload(client, "bank", "dirty.csv", dirty).json()
    assert second["run_id"] == first["run_id"]
    assert len(rows(client, "bank", "quarantine")) == 2

    reissue = upload(client, "bank", "dirty2.csv", dirty).json()
    assert reissue["merge_mode"] == "replace"
    assert len(rows(client, "bank", "quarantine")) == 2


def test_ledger_supplement_same_rules(client):
    ledger_header = "凭证号,账户,记账日期,方向,金额,摘要\n"

    def ledger_csv(txns):
        return (ledger_header + "".join(
            f"{txn},6222-0012,{day},借,{amount},货款\n" for txn, day, amount in txns
        )).encode("gb18030")

    upload(client, "ledger", "ledger_base.csv",
           ledger_csv([("V001", "2026-03-02", "100.00"), ("V002", "2026-03-03", "200.00")]))
    upload(client, "ledger", "ledger_late.csv",
           ledger_csv([("V003", "2026-03-10", "300.00")]))
    assert {row["txn_no"] for row in rows(client, "ledger")} == {"V001", "V002", "V003"}

    body = upload(client, "ledger", "ledger_reissue.csv",
                  ledger_csv([("V001", "2026-03-02", "110.00"), ("V002", "2026-03-03", "200.00")])
                  ).json()
    assert body["merge_mode"] == "replace"
    mapping = txn_map(client, "ledger")
    # 重出只覆盖 03-02..03-03，区间外的 V003 必须保留。
    assert set(mapping) == {"V001", "V002", "V003"}
    assert mapping["V001"]["amount"] == "110.00"

    full = upload(client, "ledger", "ledger_full.csv", ledger_csv([
        ("V001", "2026-03-02", "110.00"),
        ("V002", "2026-03-03", "200.00"),
        ("V003", "2026-03-10", "300.00"),
    ])).json()
    assert full["merge_mode"] == "replace"
    assert len(rows(client, "ledger")) == 3
    dirty = (
        "流水号,账户,发生时间,借贷标志,金额,手续费,对方户名,摘要\n"
        "B001,6222-0012,2026-03-02T09:00:00+08:00,借,100.00,0.00,供应商,货款\n"
        "B002,6222-0012,2026-03-03T09:00:00+08:00,借,N/A,0.00,供应商,空值\n"
        "B003,6222-0012,2026-03-04T09:00:00+08:00,X,100.00,0.00,供应商,符号错\n"
    ).encode("utf-8")
    first = upload(client, "bank", "dirty.csv", dirty).json()
    assert first["quarantine"] == 2
    assert len(rows(client, "bank", "quarantine")) == 2

    second = upload(client, "bank", "dirty.csv", dirty).json()
    assert second["run_id"] == first["run_id"]
    assert len(rows(client, "bank", "quarantine")) == 2

    reissue = upload(client, "bank", "dirty2.csv", dirty).json()
    assert reissue["merge_mode"] == "replace"
    assert len(rows(client, "bank", "quarantine")) == 2
