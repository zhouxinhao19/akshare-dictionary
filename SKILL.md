---
name: akshare-dictionary
description: >-
  Looks up AKShare financial-data APIs from a tested catalog of 1090 interfaces (stock, futures, fund, macro, option, bond, index, FX).
  Use when the user asks to 拉数据, 取数, akshare, AKShare, 数据接口, 数据字典, 行情, 基本面, 宏观, ETF, 期货, 期权, 债券, or needs the correct function name, parameters, or output columns.
---

# AKShare 数据字典

禁止凭记忆编造 `ak.*` 函数名、参数或列名。先检索本 Skill 的 catalog，再写调用代码。

Catalog 基于 AKShare **1.18.94**、2026-08-26 实测（1090 接口：成功 776 / 失败 200 / 超时 114）。`status` 是快照，不是永久不可用：东财类接口常 `ConnectionError`，文档参数仍可试。写调用前可用 `check.sh` 对候选接口现场复测。

## 工作流

```
Task Progress:
- [ ] 1. 把需求拆成：品种/市场、频率、字段、是否实时
- [ ] 2. 用 lookup.sh 检索，优先 status=success
- [ ] 3. 用 --name 看参数、实测列、example
- [ ] 4. 候选是东财 _em 类或快照较旧：用 check.sh 复测再定
- [ ] 5. 按 conventions.md 填 symbol/date/adjust
- [ ] 6. 写出 import akshare as ak 调用；列名以实测为准
```

**检索（执行，不要手读 catalog.json）：**

`scripts/...` 相对本 skill 目录（加载本 skill 时会给出 base directory）；cwd 不在这里就换成绝对路径。包装脚本会自动挑选可用的 Python（PATH 上能真正执行的 python3/python，或常见 conda 位置；Windows 商店的 `python` 占位符跑不出结果，会被自动过滤）。都不行时设环境变量 `AKSHARE_DICT_PYTHON` 指向可用 python 再试。

```bash
bash scripts/lookup.sh 日线 A股
bash scripts/lookup.sh --category futures 库存
bash scripts/lookup.sh --name stock_zh_a_hist
bash scripts/lookup.sh --status success 宏观 CPI
# 可用性存疑（尤其东财 _em 类、快照较旧）：现场复测首选和备选
bash scripts/check.sh stock_zh_a_hist stock_zh_a_daily
```

- check.sh 需要环境里有 akshare（探测时自动跳过没有的 Python）；超时默认 30s，`--timeout` 可调。

- 无命中：减少词、去掉 `--status`，或换类目名（见下表）。
- 多命中：再 `--name` 看详情。
- 多个候选：success > timeout > error；同档选描述更贴需求的。

参数约定见 [conventions.md](conventions.md)。

## 类目

| category | 内容 |
|---|---|
| stock / stock_feature / stock_fundamental | A/港/美股行情、资金、公告、财务 |
| futures / futures_derivative | 国内期货、库存、合约 |
| option | 期权行情与 T 型报价 |
| fund | 公募、ETF、净值 |
| index | 股指/行业指数 |
| bond | 国债、可转债、回购 |
| macro | 宏观、行业、经济数据 |
| interest_rate / fx / currency | 利率、外汇 |
| spot / energy | 现货、能源 |
| reits / qdii | REITs、QDII |
| others / cal / tool | 日历、杂项工具 |

## 高频需求（仍须 `--name` 核对参数）

表中「首选/备选」的可用性备注是 2026-08-26 快照，东财类尤其会变；临用前可 `check.sh 首选 备选` 复测。

| 需求 | 首选 | 备注 |
|---|---|---|
| A 股日/周/月 K | `stock_zh_a_hist` | 东财常断连；更稳：`stock_zh_a_daily`（新浪，`sz000001`）或 `stock_zh_a_hist_tx` |
| A 股分钟 | `stock_zh_a_hist_min_em` | period=`1/5/15/30/60`；1 分钟近 5 日且不复权 |
| A 股实时 | `stock_zh_a_spot_em` | 备选 `stock_zh_a_spot_tx`（实测成功） |
| A 股代码表 | `stock_info_a_code_name` | 无参；code 六位无前缀 |
| 个股资料 | `stock_individual_info_em` | symbol 六位 |
| 指数日 K | `index_zh_a_hist` | symbol 不用 `sh`/`sz` 前缀 |
| ETF 日 K | `fund_etf_hist_em` | 备选 `fund_etf_hist_sina` |
| 期货日 K | `futures_zh_daily_sina` | 连续合约如 `RB0` |
| 期货主力 | `futures_main_sina` / `match_main_contract` | 先查主力再拉日 K |
| 可转债 | `bond_zh_hs_cov_spot` / `bond_zh_hs_cov_daily` | |
| 中国 GDP | `macro_china_gdp` | |

表中没有 → lookup，不要发明相近函数名。

## 写调用时

```python
import akshare as ak

df = ak.stock_zh_a_hist(
    symbol="000001",
    period="daily",
    start_date="20240101",
    end_date="20240201",
    adjust="qfq",
)
```

- 用 catalog 的 `example` 和 `params_used` 当起点，按用户日期/代码改。
- **列名**：有 `columns` 用实测列；`columns_match_doc=false` 时不要用文档列名。
- 失败接口也可按文档参数调用，并注明实测失败原因。
- 需要最新接口列表时，让用户在字典仓库重跑 `build_dictionary.py`，再执行 `scripts/build_catalog.sh`。
