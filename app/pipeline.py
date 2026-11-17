"""平台编排：采信 → 质量 → 候选 → 匹配。

模块之间只通过本文件衔接，模块内部不互相 import 规则函数；同一批有效交易得到同一 run_id，
重跑先删后插，行数与合计不变。

导入一份补传文件时，先与上一次运行里该侧的有效集合合并（见 modules/m1_ingest/merge.py），
另一侧原样保留；页面、接口与下载文件读的都是合并后的同一批有效交易。导入在解析或合并阶段
抛错时不会写入任何数据，旧批次完整保留。
"""
from __future__ import annotations

import time

from .manifest import make_run_id, sha256_hex, utc_now_iso
from .modules.m1_ingest import service as m1_service
from .modules.m1_ingest import merge as merge_mod
from .modules.m2_contract import service as m2_service
from .modules.m3_quality import quarantine as quarantine_mod
from .modules.m3_quality import rules as quality_rules
from .modules.m4_candidates import candidate as candidate_mod
from .modules.m4_candidates import window as window_mod
from .modules.m5_matching import solve as solve_mod
from .modules.m6_diff import differences as differences_mod

DEFAULT_BANK = "bank_statement_dirty.csv"
DEFAULT_LEDGER = "ledger_book_dirty.csv"

NOOP_STATES = ("EMPTY_FILE", "HEADER_ONLY", "ALL_BLANK", "HEADER_MISSING")


def _parse(settings, side: str, filename: str, raw: bytes) -> tuple[dict, list[dict]]:
    aliases = m2_service.alias_index(side)
    parsed = m1_service.read_and_map(settings, side, filename, raw, aliases)
    records = []
    if not parsed["state"]:
        records = quality_rules.build_records(side, parsed["records"], settings)
    return parsed, records


def _stamp(records: list[dict], filename: str, sha: str, seq: int) -> None:
    for record in records:
        record["source_file"] = filename
        record["source_sha"] = sha
        record["source_seq"] = seq
        record.setdefault("origin_sha", sha)


def _side_meta(parsed: dict) -> dict:
    return {
        "file": parsed["filename"],
        "sha": parsed["sha256"] if "sha256" in parsed else "",
        "bytes": parsed["bytes"],
        "raw": parsed["raw_rows"],
        "state": parsed["state"] or "OK",
        "segments": parsed["segments"],
    }


def _counts(records: list[dict]) -> dict:
    bank = [record for record in records if record["side"] == "bank"]
    ledger = [record for record in records if record["side"] == "ledger"]
    return {
        "bank_raw": len(bank),
        "ledger_raw": len(ledger),
        "bank_valid": sum(1 for record in bank if record["status"] == "valid"),
        "ledger_valid": sum(1 for record in ledger if record["status"] == "valid"),
        "bank_quar": sum(1 for record in bank if record["status"] == "quarantine"),
        "ledger_quar": sum(1 for record in ledger if record["status"] == "quarantine"),
    }


def _empty_meta() -> dict:
    return {"file": "", "sha": "", "bytes": 0, "raw": 0, "state": "", "segments": 0}


def _run(settings, bank_meta: dict, ledger_meta: dict, records: list[dict],
         status: str, duration_ms: int = 0) -> dict:
    counts = _counts(records)
    params = settings.params()
    return {
        "run_id": make_run_id(bank_meta.get("eff_sha", ""), ledger_meta.get("eff_sha", ""),
                              settings.rule_version, params),
        "created_at": utc_now_iso(),
        "rule_version": settings.rule_version,
        "params": params,
        "bank_sha256": bank_meta["sha"],
        "ledger_sha256": ledger_meta["sha"],
        "bank_eff_sha256": bank_meta.get("eff_sha", ""),
        "ledger_eff_sha256": ledger_meta.get("eff_sha", ""),
        "bank_file": bank_meta["file"],
        "ledger_file": ledger_meta["file"],
        "bank_bytes": bank_meta["bytes"],
        "ledger_bytes": ledger_meta["bytes"],
        "bank_raw": counts["bank_raw"],
        "ledger_raw": counts["ledger_raw"],
        "bank_valid": counts["bank_valid"],
        "ledger_valid": counts["ledger_valid"],
        "bank_quar": counts["bank_quar"],
        "ledger_quar": counts["ledger_quar"],
        "bank_merge": bank_meta.get("merge", {}),
        "ledger_merge": ledger_meta.get("merge", {}),
        "bank_sources": bank_meta.get("sources", []),
        "ledger_sources": ledger_meta.get("sources", []),
        "segments": bank_meta["segments"] + ledger_meta["segments"],
        "status": status,
        "input_states": {"bank": bank_meta["state"], "ledger": ledger_meta["state"]},
        "duration_ms": duration_ms,
    }


def _summary(settings, run: dict, records: list[dict], candidates: list[dict],
             matches: list[dict]) -> dict:
    reconciliation = differences_mod.reconcile(records, matches, settings)
    return {
        "run_id": run["run_id"],
        "status": run["status"],
        "created_at": run["created_at"],
        "counts": reconciliation["counts"],
        "totals": {key: format(value, "f") for key, value in reconciliation["totals"].items()},
        "candidates": len(candidates),
        "matches": len(matches),
        "quarantine_distribution": quarantine_mod.distribution(records),
        "input_states": run["input_states"],
        "bank_sha256": run["bank_sha256"],
        "ledger_sha256": run["ledger_sha256"],
        "bank_eff_sha256": run.get("bank_eff_sha256", ""),
        "ledger_eff_sha256": run.get("ledger_eff_sha256", ""),
        "merge": {"bank": run.get("bank_merge", {}), "ledger": run.get("ledger_merge", {})},
        "sources": {"bank": run.get("bank_sources", []), "ledger": run.get("ledger_sources", [])},
    }


def _finalize(settings, bank_meta: dict, ledger_meta: dict, records: list[dict],
              status: str, duration: int, store) -> tuple[dict, list[dict], list[dict]]:
    candidates: list[dict] = []
    matches: list[dict] = []
    if bank_meta.get("eff_sha") and ledger_meta.get("eff_sha"):
        candidates = candidate_mod.build(records, settings)
        matches = solve_mod.solve(candidates, records, settings)
        status = "matched"
    run = _run(settings, bank_meta, ledger_meta, records, status, duration)
    store.save_run(run, records, candidates, matches)
    return run, candidates, matches


def run_demo(settings, store, bank_name: str = DEFAULT_BANK,
             ledger_name: str = DEFAULT_LEDGER) -> dict:
    """样例全量运行：两份文件都是一次性基准，不并入历史。"""
    started = time.monotonic()
    bank_file, bank_raw = m1_service.resolve_sample(settings, bank_name)
    ledger_file, ledger_raw = m1_service.resolve_sample(settings, ledger_name)
    parsed_bank, bank_records = _parse(settings, "bank", bank_file, bank_raw)
    parsed_ledger, ledger_records = _parse(settings, "ledger", ledger_file, ledger_raw)
    parsed_bank["sha256"] = sha256_hex(bank_raw)
    parsed_ledger["sha256"] = sha256_hex(ledger_raw)
    _stamp(bank_records, bank_file, parsed_bank["sha256"], 1)
    _stamp(ledger_records, ledger_file, parsed_ledger["sha256"], 1)
    records = bank_records + ledger_records

    bank_meta = _side_meta(parsed_bank)
    ledger_meta = _side_meta(parsed_ledger)
    bank_meta.update({
        "eff_sha": merge_mod.effective_sha256(bank_records),
        "merge": {"side": "bank", "mode": merge_mod.MODE_INITIAL,
                  "new_rows": len(bank_records), "effective_rows": len(bank_records)},
        "sources": [{"file": bank_file, "sha": bank_meta["sha"], "seq": 1,
                     "rows": len(bank_records), "state": bank_meta["state"] or "OK"}],
    })
    ledger_meta.update({
        "eff_sha": merge_mod.effective_sha256(ledger_records),
        "merge": {"side": "ledger", "mode": merge_mod.MODE_INITIAL,
                  "new_rows": len(ledger_records), "effective_rows": len(ledger_records)},
        "sources": [{"file": ledger_file, "sha": ledger_meta["sha"], "seq": 1,
                     "rows": len(ledger_records), "state": ledger_meta["state"] or "OK"}],
    })
    bank_meta["bytes"] = len(bank_raw)
    ledger_meta["bytes"] = len(ledger_raw)

    duration = int((time.monotonic() - started) * 1000)
    run, candidates, matches = _finalize(settings, bank_meta, ledger_meta, records,
                                         "matched", duration, store)
    return _summary(settings, store.get_run(run["run_id"]), records, candidates, matches)


def _previous_side(previous: dict | None, side: str, store) -> tuple[list[dict], dict, list]:
    """取上一次运行里某一侧的有效集合、输入元信息与已导入来源。"""
    if not previous or not previous.get(f"{side}_sha256"):
        return [], _empty_meta(), []
    meta = {
        "file": previous.get(f"{side}_file", ""),
        "sha": previous.get(f"{side}_sha256", ""),
        "eff_sha": previous.get(f"{side}_eff_sha256", ""),
        "bytes": previous.get(f"{side}_bytes", 0),
        "raw": previous.get(f"{side}_raw", 0),
        "state": previous.get("input_states", {}).get(side, "OK"),
        "segments": previous.get("segments", 0),
        "merge": previous.get(f"{side}_merge", {}),
        "sources": previous.get(f"{side}_sources", []),
    }
    return store.records(previous["run_id"], side=side), meta, meta["sources"]


def _source_seq(sources: list[dict], sha: str) -> int:
    for source in sources:
        if source.get("sha") == sha:
            return int(source.get("seq") or 1)
    return max((int(source.get("seq") or 0) for source in sources), default=0) + 1


def _track_sources(sources: list[dict], filename: str, sha: str, seq: int,
                   parsed: dict, merged: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for record in merged:
        key = record.get("source_sha") or ""
        counts[key] = counts.get(key, 0) + 1
    by_seq = {int(source.get("seq") or 0): dict(source) for source in sources}
    by_seq[seq] = {
        "file": filename,
        "sha": sha,
        "seq": seq,
        "bytes": parsed["bytes"],
        "rows": len(parsed["records"]),
        "state": parsed["state"] or "OK",
    }
    for entry in by_seq.values():
        entry["active"] = counts.get(entry.get("sha", ""), 0) > 0
        entry["rows_in_set"] = counts.get(entry.get("sha", ""), 0)
    return [by_seq[key] for key in sorted(by_seq)]


def _structural_risk(records: list[dict]) -> str | None:
    """文件本身是否有结构性缺陷；有缺陷的补传不得当作「整段重出」去撤回旧行。

    单行脏数据（某行多一列）不构成风险，末行截断或大面积列数不齐才说明这次导出本身不完整。
    """
    if not records:
        return None
    if any("TRUNCATED_TAIL" in record.get("flags", []) for record in records):
        return "TRUNCATED_TAIL"
    broken = sum(1 for record in records if "COLUMN_COUNT" in record.get("flags", []))
    if broken * 5 >= len(records):
        return "COLUMN_COUNT"
    return None


def import_side(settings, store, side: str, filename: str, raw: bytes,
                mode: str = "auto") -> dict:
    started = time.monotonic()
    parsed, records = _parse(settings, side, filename, raw)
    sha = sha256_hex(raw)
    parsed["bytes"] = len(raw)
    parsed["sha256"] = sha

    previous = store.latest_run()
    other = "ledger" if side == "bank" else "bank"
    other_records, other_meta, _ = _previous_side(previous, other, store)
    prev_side_records, prev_meta, prev_sources = _previous_side(previous, side, store)

    seq = _source_seq(prev_sources, sha)
    _stamp(records, filename, sha, seq)

    zone = window_mod.tzinfo(settings.tz_name)
    empty_state = parsed["state"] in NOOP_STATES
    if empty_state:
        # 空文件/仅表头/全空行不构成一次有效补传：保留该侧旧版，只记录这次尝试。
        merged_side = prev_side_records
        report = {
            "side": side,
            "mode": "noop",
            "state": parsed["state"],
            "new_rows": len(records),
            "replaced_rows": 0,
            "dropped_rows": 0,
            "kept_old_rows": len(prev_side_records),
            "superseded_new_rows": 0,
            "effective_rows": len(merged_side),
            "accounts": [],
        }
    else:
        # 结构损坏（末行截断/列数不齐）的补传可能是坏导出：只补新增，绝不用它撤回旧行。
        risk = _structural_risk(records)
        effective_mode = "cumulative" if risk and mode == "auto" else mode
        merged_side, report = merge_mod.merge_side(
            side, prev_side_records, records, zone, seq, effective_mode
        )
        report["state"] = None
        report["structural_guard"] = risk

    sources = _track_sources(prev_sources, filename, sha, seq, parsed, merged_side)

    side_meta = _side_meta(parsed)
    side_meta.update({
        "file": filename,
        "sha": sha,
        "eff_sha": merge_mod.effective_sha256(merged_side),
        "merge": report,
        "sources": sources,
    })
    if empty_state and prev_meta.get("sha"):
        # 该侧仍是旧版文件；输入元信息保持指向最后一次真正生效的文件。
        side_meta["sha"] = prev_meta["sha"]
        side_meta["file"] = prev_meta.get("file", "")
        side_meta["eff_sha"] = prev_meta.get("eff_sha") or side_meta["eff_sha"]

    if side == "bank":
        bank_meta, ledger_meta = side_meta, other_meta
    else:
        bank_meta, ledger_meta = other_meta, side_meta

    combined = other_records + merged_side
    both = bool(bank_meta.get("eff_sha") and ledger_meta.get("eff_sha"))
    status = "matched" if both else f"partial_{side}"
    duration = int((time.monotonic() - started) * 1000)
    run, candidates, matches = _finalize(settings, bank_meta, ledger_meta, combined,
                                         status, duration, store)
    summary = _summary(settings, store.get_run(run["run_id"]), combined, candidates, matches)
    summary.update({
        "side": side,
        "state": parsed["state"],
        "encoding": parsed["encoding"],
        "bom": parsed["bom"],
        "segments": parsed["segments"],
        "raw_rows": parsed["raw_rows"],
        "valid": sum(1 for record in records if record["status"] == "valid"),
        "quarantine": sum(1 for record in records if record["status"] == "quarantine"),
        "merge_mode": report["mode"],
        "merge": report,
    })
    return summary
