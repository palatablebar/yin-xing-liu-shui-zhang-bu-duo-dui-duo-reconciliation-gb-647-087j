"""同一侧多份补传文件的合并：判定「整段重出」还是「只补新增」。

补传有两种形态，且两份文件的日期范围会重叠：

- 整段重出：同一账户同一段时间被重新导出一遍。新文件里同一流水号的副本覆盖旧副本；
  在被重出的日期段内，旧文件有、新文件不再列出的流水视为已更正/撤回。
- 只补新增：新文件只带来晚到的流水，日期可能落在旧文件的区间里，旧文件独有流水必须保留。

判定只看业务数据，不看文件名与上传顺序，逐账户比较「被重述的来源」的日期覆盖与流水号集合：

- 重述来源在新文件日期段内的流水号全部出现在新文件里 → 整段重出（replace）；
- 重述来源的日期段完整落在新文件区间内，且覆盖率达标 → 整段重出；
- 与旧集合没有任何共同流水号，或只重述少数旧行 → 只补新增（cumulative），不删旧行。

「重述来源」是旧集合里至少有一条流水号出现在本次文件中的那个来源文件。每条流水带
`origin_sha` 记录最初来源，替换不改写它；因此一份与本次文件毫无交集的独立补传（晚到扣款），
即使日期落在重出段内也不会被撤回。

去重只认业务号（银行流水号 / 凭证号），不认金额与摘要：银行同一天存在两笔同额同摘要
扣款时，只要流水号不同就各自保留。同一流水号在文件内出现多次（取值冲突）时整组保留，
「有效行 + 冲突证据行」不会被拆散。

上一版来源文件如果比本次文件更新（导入序号更大），重叠流水号保留上一版，避免用旧文件
覆盖已更正的补传；同一文件重跑导入复用原导入序号，因此不会新增行或重复计数。
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date, datetime, timezone

MODE_INITIAL = "initial"
MODE_REPLACE = "replace"
MODE_CUMULATIVE = "cumulative"
MODE_MIXED = "mixed"

# 新文件完整包住旧区间时，旧流水号至少被重述到这个比例才判整段重出。
REPLACE_COVERAGE = 0.8


def _local_date(record: dict, zone) -> date | None:
    text = record.get("posted_at") or ""
    if not text:
        return None
    try:
        instant = datetime.fromisoformat(text)
    except ValueError:
        return None
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(zone).date()


def group_key(record: dict) -> tuple:
    """业务身份：有流水号按流水号；没有流水号的隔离行按来源文件 + 行号各自独立。"""
    txn_no = (record.get("txn_no") or "").strip()
    if txn_no:
        return ("txn", txn_no)
    return ("row", record.get("source_sha", ""), record.get("row_no"))


def _group(records: list[dict]) -> tuple[dict[tuple, list[dict]], list[tuple]]:
    groups: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    for record in records:
        key = group_key(record)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(record)
    return groups, order


def _source_seq(record: dict) -> int:
    try:
        return int(record.get("source_seq") or 0)
    except (TypeError, ValueError):
        return 0


def _origin(record: dict) -> str:
    return record.get("origin_sha") or record.get("source_sha") or ""


def _decide(previous: list[dict], fresh: list[dict], zone) -> dict[str, dict]:
    """逐账户判定本次补传的形态。返回 account -> 判定明细。

    「重述来源」= 旧集合里至少有一条流水号出现在本次文件中的那个来源文件。覆盖率只在
    重述来源的行上计算：与本次文件毫无交集的独立补传（晚到扣款）既不算被撤回的行，
    也不参与「是否整段重出」的判定。
    """
    prior_by_account: dict[str, list[tuple[date, dict]]] = defaultdict(list)
    for record in previous:
        day = _local_date(record, zone)
        if day is not None:
            prior_by_account[record.get("account", "")].append((day, record))

    fresh_by_account: dict[str, list[tuple[date, dict]]] = defaultdict(list)
    for record in fresh:
        day = _local_date(record, zone)
        if day is not None:
            fresh_by_account[record.get("account", "")].append((day, record))

    decisions: dict[str, dict] = {}
    for account, fresh_rows in fresh_by_account.items():
        new_days = [day for day, _ in fresh_rows]
        start, end = min(new_days), max(new_days)
        new_keys = {row["txn_no"] for _, row in fresh_rows if row.get("txn_no")}
        prior = prior_by_account.get(account, [])
        if not prior:
            decisions[account] = {
                "mode": MODE_INITIAL,
                "range": [start.isoformat(), end.isoformat()],
                "prior_rows": 0,
                "prior_keys": 0,
                "shared_keys": 0,
                "restated_sources": [],
            }
            continue

        restated = {
            _origin(row) for _, row in prior
            if _origin(row) and row.get("txn_no") in new_keys
        }
        relevant = [(day, row) for day, row in prior if _origin(row) in restated]
        relevant_days = [day for day, _ in relevant] or [day for day, _ in prior]
        prior_start, prior_end = min(relevant_days), max(relevant_days)
        overlapped = [(day, row) for day, row in relevant if start <= day <= end]
        prior_keys = {row["txn_no"] for _, row in overlapped if row.get("txn_no")}
        shared = prior_keys & new_keys

        mode = MODE_CUMULATIVE
        if not restated:
            # 本次文件与旧集合没有任何流水号交集：纯新增，绝不撤回旧行。
            mode = MODE_CUMULATIVE
        elif prior_keys and prior_keys <= new_keys:
            # 重叠段内的旧流水全部被新文件重述，这一段是整段重出。
            mode = MODE_REPLACE
        elif (prior_start >= start and prior_end <= end and prior_keys
              and len(shared) >= max(1, int(len(prior_keys) * REPLACE_COVERAGE + 0.999))):
            # 重述来源完整包住旧区间，且绝大多数旧流水被重述，按整段重出处理。
            mode = MODE_REPLACE

        decisions[account] = {
            "mode": mode,
            "range": [start.isoformat(), end.isoformat()],
            "prior_rows": len(prior),
            "prior_keys": len(prior_keys),
            "shared_keys": len(shared),
            "restated_sources": sorted(restated),
        }
    return decisions


def _overall_mode(decisions: dict[str, dict], had_previous: bool) -> str:
    modes = {item["mode"] for item in decisions.values()}
    decisive = modes - {MODE_INITIAL}
    if not decisive:
        return MODE_INITIAL if not had_previous else MODE_CUMULATIVE
    if MODE_REPLACE in decisive and MODE_CUMULATIVE in decisive:
        return MODE_MIXED
    return MODE_REPLACE if MODE_REPLACE in decisive else MODE_CUMULATIVE


def _within(record: dict, zone, start: date, end: date) -> bool:
    day = _local_date(record, zone)
    return day is not None and start <= day <= end


def merge_side(side: str, previous: list[dict], fresh: list[dict], zone,
               fresh_seq: int, forced_mode: str | None = None) -> tuple[list[dict], dict]:
    """把新文件并入该侧已有有效集合。

    previous 是上一次运行里该侧的完整集合（有效行 + 隔离证据），fresh 是本次文件解析出的行。
    forced_mode 为 replace / cumulative 时跳过自动判定；auto（None）按业务数据判定。
    返回 (合并后的行, 判定报告)；报告说明本次是替换还是累计，以及各类行数。
    """
    prev_groups, prev_order = _group(previous)
    new_groups, new_order = _group(fresh)
    decisions = _decide(previous, fresh, zone)
    if forced_mode in (MODE_REPLACE, MODE_CUMULATIVE):
        for item in decisions.values():
            item["forced"] = True
            if item["mode"] == MODE_INITIAL:
                # 该账户第一次出现，没有旧段可替换，仍按首份基准处理。
                continue
            item["mode"] = forced_mode

    replace_spans = {
        account: (date.fromisoformat(item["range"][0]), date.fromisoformat(item["range"][1]))
        for account, item in decisions.items()
        if item["mode"] == MODE_REPLACE
    }

    # 只有「本次文件正在重述的来源」才允许被撤回行。旧来源与本次文件一个流水号都不相交时，
    # 它是一份独立的补传（晚到扣款），即使落在重出日期段内也必须保留。
    # origin_sha 记录流水最初出自哪份文件，替换不会改写它，因此补传文件的行不会被误认成基准行。
    restated_sources: set[str] = set()
    for item in decisions.values():
        restated_sources.update(item.get("restated_sources", []))
    if not restated_sources:
        # 没有来源标记的旧数据：退化为按账户判定，保证升级前的库仍能重出。
        restated_sources = {row.get("source_sha") or "" for row in previous}

    dropped_keys: set[tuple] = set()
    superseded_new_rows = 0
    for key, rows in prev_groups.items():
        if key in new_groups or key[0] != "txn":
            continue
        account = rows[0].get("account", "")
        if any(row.get("account", "") != account for row in rows):
            continue
        span = replace_spans.get(account)
        if span is None:
            continue
        origins = {row.get("origin_sha") or row.get("source_sha") or "" for row in rows}
        if not origins & restated_sources:
            continue
        if any(_source_seq(row) > fresh_seq for row in rows):
            # 旧行来自更晚的补传，本次重出不得把它撤回；同一文件重跑不算更晚。
            continue
        if all(_within(row, zone, span[0], span[1]) for row in rows):
            dropped_keys.add(key)

    # 无流水号的隔离证据（缺字段、日期错、账户未登记）无法按键对应。整份旧文件被重出时，
    # 这些遗留证据也应随旧版一起退场，否则每次重出都会多堆一份同样的隔离行。
    by_source: dict[str, list[tuple]] = defaultdict(list)
    for key, rows in prev_groups.items():
        sha = rows[0].get("origin_sha") or rows[0].get("source_sha") or ""
        if sha and sha in restated_sources:
            by_source[sha].append(key)
    for keys in by_source.values():
        rows_of_source = [row for key in keys for row in prev_groups[key]]
        if any(_source_seq(row) > fresh_seq for row in rows_of_source):
            continue
        settled = [key for key in keys if key[0] == "txn" and (key in new_groups or key in dropped_keys)]
        unsettled = [key for key in keys if key[0] == "txn" and key not in new_groups
                     and key not in dropped_keys]
        if settled and not unsettled:
            dropped_keys.update(key for key in keys if key[0] == "row" and key not in new_groups)

    merged: list[dict] = []
    replaced_rows = 0
    for key in prev_order:
        old_rows = prev_groups[key]
        if key in dropped_keys:
            continue
        if key in new_groups:
            if any(_source_seq(row) > fresh_seq for row in old_rows):
                merged.extend(old_rows)
                superseded_new_rows += len(new_groups[key])
                continue
            refreshed = []
            for row in new_groups[key]:
                row = dict(row)
                inherited = next(
                    (old.get("origin_sha") for old in old_rows if old.get("txn_no") == row.get("txn_no")
                     and (old.get("source_sha") or "") in restated_sources),
                    "",
                )
                # 重述行沿用原文件的归属，后续重出才能继续撤回同一份来源的行。
                row["origin_sha"] = inherited or row.get("origin_sha") or row.get("source_sha", "")
                refreshed.append(row)
            merged.extend(refreshed)
            replaced_rows += len(old_rows)
            continue
        merged.extend(old_rows)
    for key in new_order:
        if key not in prev_groups:
            merged.extend(new_groups[key])

    merged.sort(
        key=lambda row: (
            row.get("account", ""),
            row.get("posted_at") or "￿",
            row.get("txn_no", ""),
            0 if row.get("status") == "valid" else 1,
        )
    )
    for seq, row in enumerate(merged):
        row["seq"] = seq

    dropped_rows = sum(len(prev_groups[key]) for key in dropped_keys)
    kept_old_rows = len(previous) - replaced_rows - dropped_rows

    accounts: list[dict] = []
    for account, item in decisions.items():
        account_prior = [row for rows in prev_groups.values() for row in rows
                         if row.get("account", "") == account]
        account_new = [row for rows in new_groups.values() for row in rows
                       if row.get("account", "") == account]
        account_replaced = sum(
            1 for key, rows in prev_groups.items() if key in new_groups
            for row in rows if row.get("account", "") == account
        )
        account_dropped = sum(
            1 for key in dropped_keys for row in prev_groups[key]
            if row.get("account", "") == account
        )
        accounts.append({
            "account": account,
            "mode": item["mode"],
            "range": item["range"],
            "prior_rows": item["prior_rows"],
            "prior_keys": item["prior_keys"],
            "shared_keys": item["shared_keys"],
            "new_rows": len(account_new),
            "replaced_rows": account_replaced,
            "dropped_rows": account_dropped,
            "kept_old_rows": len(account_prior) - account_replaced - account_dropped,
        })

    report = {
        "side": side,
        "mode": _overall_mode(decisions, bool(previous)),
        "forced_mode": forced_mode or "auto",
        "accounts": accounts,
        "new_rows": len(fresh),
        "replaced_rows": replaced_rows,
        "dropped_rows": dropped_rows,
        "kept_old_rows": kept_old_rows,
        "superseded_new_rows": superseded_new_rows,
        "effective_rows": len(merged),
    }
    return merged, report


def _canonical(record: dict) -> list:
    """有效集合的身份字段：只含业务内容，不含行号、来源与导入顺序。"""
    return [
        record.get("side", ""),
        record.get("txn_no", ""),
        record.get("account", ""),
        record.get("posted_at", ""),
        int(record.get("direction") or 0),
        str(record.get("amount", "")),
        str(record.get("fee", "")),
        record.get("memo", ""),
        record.get("counterparty", ""),
        record.get("status", ""),
        record.get("reason", ""),
        sorted(record.get("flags", [])),
    ]


def effective_sha256(records: list[dict]) -> str:
    """一批有效交易的内容摘要；同一批交易无论由哪些文件补传而来，摘要一致。

    空集合返回空串，避免「空文件」被当成一个存在的数据源参与对账。
    """
    if not records:
        return ""
    payload = [_canonical(record) for record in records]
    payload.sort()
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
