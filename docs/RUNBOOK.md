# 运行手册 ledgerbank

## 启动与停止

```bash
docker compose up -d --build
docker compose ps
curl -s http://127.0.0.1:18206/api/health
docker compose down -v --remove-orphans
```

## 重建数据库

```bash
docker compose down -v
docker compose up -d --build
```

命名卷 `ledgerbank-data` 保存批次、流水行、候选与匹配。重建后从「载入样例」重新开始。

## 主流程

1. `POST /api/demo/run`：读取两份脏样例，采信、质量、候选、匹配一次跑完。
2. `GET /api/quarantine`：查看隔离行与原因分布。
3. `GET /api/candidates`：查看已生成候选。
4. `GET /api/matches`：查看匹配组与手续费分摊。
5. `GET /api/differences`：查看两侧守恒与差异归因。
6. `GET /api/export/report.csv` 与 `GET /api/export/manifest.json`：导出。

## 单步重跑

```bash
curl -s -X POST http://127.0.0.1:18206/api/candidates
curl -s -X POST http://127.0.0.1:18206/api/matches
```

同一输入重跑命中同一 `run_id`，先删后插，行数与合计不变。

## 导入自有文件

```bash
curl -s -X POST http://127.0.0.1:18206/api/statements/import \
  -F side=bank -F file=@sample_data/bank_statement_dirty.csv
```

支持 UTF-8（含 BOM）、UTF-8 无 BOM、GB18030；列名按 `GET /api/contract` 的别名表映射。

## 补传与重出

同一账户的流水会分批补传，两份文件的日期范围可能重叠：

- 默认 `mode=auto`：逐账户比较日期覆盖与流水号集合，自动判定「整段重出」还是「只补新增」。
- 判定不确定时显式指定：`-F mode=replace`（整段重出，同段旧流水被替换）或
  `-F mode=cumulative`（只补新增，旧文件独有流水保留）。
- 响应里的 `merge_mode` 与 `merge` 字段说明本次判定结果：新增 / 替换 / 撤回各多少行。
- 整段重出只撤回「本次文件正在重述的来源」里、落在重出日期段内的流水；来自其他补传文件的
  晚到扣款不受影响。
- 导入失败（404、坏文件、落库异常）不写入任何数据，旧批次完整保留。
- 空文件、仅表头、全空行返回 `noop`，不会清空该侧旧版，只会记录一次无效尝试。
- 同一组文件重跑导入，`run_id`、行数、合计都不变。

查看某次导入的判定与来源追溯：

```bash
curl -s http://127.0.0.1:18206/api/export/manifest.json | python3 -m json.tool
```

`inputs.bank.merge` 是本次判定，`inputs.bank.sources` 按导入序号列出所有补传文件及是否仍生效。

## 失败处理

- 400：参数或文件不可用，页面顶部提示原因，保留已填内容。
- 404：批次或样例不存在，先执行 `POST /api/demo/run`。
- 409：还没有候选就求解匹配，先执行 `POST /api/candidates`。
- 隔离行不是失败：按原因码修正源文件后重新导入。

## 日志

```bash
docker compose logs -f app
```
