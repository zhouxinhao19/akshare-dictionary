# AKShare 参数与代码约定

写调用前读对应接口的 `--name` 详情；本节只覆盖跨接口的常见坑。以该接口 `params.desc` / `example` 为准。

## 证券代码

| 市场 | 写法 | 例 |
|---|---|---|
| 沪深京 A 股 | 默认 6 位无前缀 | `000001` `600519` |
| 指数 | 6 位，无市场前缀 | `000300` `000016` `399006` |
| ETF / 基金 | 6 位 | `510300` `159915` |
| 港股 | 5 位（部分接口 5 位或带 `.HK`） | `00700` |
| 美股 | ticker | `AAPL` |
| 可转债 | 6 位 | `113050` |
| 国内期货 | 品种+合约月，或连续 `品种0` | `rb2505` `RB0` |
| 外汇 | 货币对 | `USDCNY` |

不要默认 `000001.SZ`。按该接口 example：

- 东财 `*_em` / `stock_zh_a_hist`：六位 `000001`
- 新浪 `stock_zh_a_daily`、腾讯 `stock_zh_a_hist_tx`：带市场前缀 `sz000001` / `sh600519`
- `ts_code`：`000001.SZ`；`secid`：深市 `0.000001`，沪市 `1.600519`

## 日期

| 场景 | 格式 | 例 |
|---|---|---|
| 日频起止（多数 `*_hist`） | `YYYYMMDD` | `20240101` |
| 分钟线起止 | `YYYY-MM-DD HH:MM:SS` | `2024-03-20 09:30:00` |
| 单日事件/日历 | 看 desc，常见 `YYYYMMDD` | `20241107` |

用户给 `2024-01-01` 时，按目标接口格式转换，不要混用。

## 周期与复权

- 日频 `period`：`daily` / `weekly` / `monthly`（字符串，不是 `D`/`W`）。
- 分钟 `period`：`'1'` `'5'` `'15'` `'30'` `'60'`（字符串数字）。
- `adjust`：`""` 不复权，`qfq` 前复权，`hfq` 后复权。用户没说时日频默认 `qfq`，分钟按 example。
- 1 分钟数据通常近 5 个交易日且不复权。

## 期货

- 连续合约：品种大写 + `0`，如螺纹 `RB0`、沪铜 `CU0`。
- 具体合约大小写以该接口 example 为准（新浪日 K 常用 `RB0`）。
- 部分库存/现货接口 `symbol` 是中文（`黄金`、`螺纹钢`），不要改成代码。
- 未知主力：先 `match_main_contract` 或 `futures_main_sina`，再拉行情。

## 列名与单位

- 优先实测 `columns`。文档列常缺或与返回不一致。
- 东财行情常见中文列：`日期, 开盘, 收盘, 最高, 最低, 成交量, 成交额, 振幅, 涨跌幅, 涨跌额, 换手率`。
- 新浪期货日 K 常见英文列：`date, open, high, low, close, volume, hold, settle`。
- `成交量` 单位常为**手**；`成交额` 常为**元**；涨跌幅/换手率常为 **%**。以 desc 为准，不要自行换算除非用户要求。

## 调用稳定性

- 东财（`_em`）快照里失败多，新浪（`_sina`）/腾讯（`_tx`）相对稳。同类需求优先 success，再 fallback。
- 实时接口盘后仍返回当日快照；历史日 K「当日收盘价请收盘后取」。
- 设置合理日期区间，避免一次拉超长历史除非用户要。
- `timeout` 仅在接口声明该参数时传入。

## 更新 catalog

推荐从已安装的 AKShare 1.20+ 内置接口表重建：

```bash
python scripts/build_catalog.py
```

仍可使用旧字典仓库生成的 `akshare_dictionary.json`：

```bash
python scripts/build_catalog.py --src <字典仓库>/output/akshare_dictionary.json
```

`--src` 必须指向字典仓库的 `akshare_dictionary.json`（原脚本里的 `~/Desktop/akshare数据字典/...` 只是本机的历史默认值，换机器后不可用）。

可选标志：`--keep-status` 无条件继承全部实测状态；`--no-inherit` 全部标 untested。

重建后可用 `python scripts/drift.py` 做离线漂移检查，再用 `python scripts/retest.py --status untested` 复测未测或参数已变的接口。
