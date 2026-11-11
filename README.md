# akshare-dictionary · AKShare 数据字典 Skill

一个可导入 AI 编程工具（ZCode / Cursor 等支持 Agent Skills 的工具）的 **Skill**，内置 **1072 个 AKShare 接口的版本化实测目录**，并提供候选接口的现场复测与定时在线烟测。让大模型写 AKShare 取数代码前先查目录，**避免编造 `ak.*` 函数名、参数和列名**。

> LLM 写 AKShare 代码最常见的失败不是不会写，而是**自信地编造**：函数名不存在、参数顺序错、列名和文档对不上。本 Skill 用一份实测过的接口目录 + 检索/复测脚本把这一步变成"先查再写"。

## 目录里有什么

`data/catalog.json`（约 1.4MB）由全量接口实测生成，每个接口记录：

| 字段 | 内容 |
|---|---|
| `desc` / `limit` | 接口用途与注意事项 |
| `params` | 参数名、类型、说明（来自文档） |
| `params_used` | 实测时真实使用的参数值（可直接照抄起步） |
| `columns` | **实测返回列名**（文档列经常缺失或对不上） |
| `status` | 实测结果：success / error / timeout（快照，可用 `check.sh` 复测） |
| `example` | 可运行的调用样例 |

快照信息：AKShare **1.20.37**，2026-11-10 实测，1072 接口（成功 768 / 失败 193 / 超时 111），覆盖 stock / futures / fund / option / bond / index / macro / fx / spot 等类目。该目录不是实时数据服务；仓库 GitHub Actions 每 6 小时复测一组代表性接口，结果保存在 Actions artifact 中。

## 安装

把整个文件夹复制到工具的 skills 目录即可（无需安装依赖；检索只用 Python 标准库）：

```bash
# ZCode（用户级，推荐）
git clone https://github.com/<OWNER>/akshare-dictionary.git ~/.agents/skills/akshare-dictionary

# Cursor
git clone https://github.com/<OWNER>/akshare-dictionary.git ~/.cursor/skills/akshare-dictionary
```

脚本会自动挑选可用的 Python（PATH 上能真正执行的 python3/python，或常见 conda 位置；Windows 商店的 `python` 占位符会被自动过滤）。都不行时设 `AKSHARE_DICT_PYTHON` 指向可用 python。`check.sh` 现场复测需要环境里装有 akshare。

## 用法（通常由 Agent 自动触发）

提到"拉数据 / 取数 / akshare / 行情 / ETF / 期货"等关键词即触发；也可以手动：

```bash
# 检索：自然语言关键词
bash scripts/lookup.sh 日线 A股
bash scripts/lookup.sh --category futures 库存

# 看某接口的参数、实测列、调用样例
bash scripts/lookup.sh --name stock_zh_a_hist

# 只看实测成功的
bash scripts/lookup.sh --status success 宏观 CPI

# 现场复测可用性（catalog 的 status 是快照，东财类接口时好时坏）
bash scripts/check.sh stock_zh_a_hist stock_zh_a_daily
```

复测代表性接口组（需要安装 akshare）：

```bash
python scripts/smoke.py                  # 分 6 组，每组多个候选，任一成功即该组可用
python scripts/smoke.py --json           # 机读报告
python scripts/smoke.py --timeout 60 --retries 1
```

烟测的设计取舍：
- **只验证连通性**：每组候选都改用最近 30 天的窄日期区间，不照抄 catalog 里为记录列名而用的宽区间（否则单接口翻全历史，容易被误判超时）。
- **组内任一候选成功即通过**：东财、新浪、腾讯互为主备，单个数据源抖动不该让整组报红。
- **每组的全部候选都会探一遍**：报告里能直接看出是哪个数据源挂了。
- **整组全挂才重试首选**：一次重试用来区分「真故障」和「网络抖动」。
- `index_zh_a_hist` 每次调用前要分页拉东方财富指数量表（约 17 页、页间 sleep），冷启动固定 30s 左右，脚本内对它有单独的超时放宽。

参数填写约定（证券代码格式、日期格式、复权、期货连续合约等跨接口的坑）见 [conventions.md](conventions.md)。

## 重建目录

推荐从已安装的 AKShare 1.20+ 内置接口表重建。实测状态按参数指纹继承：接口名和测试参数没变时沿用上一版 status/columns，只有新增或参数变更的接口会标成 untested。

```bash
python scripts/build_catalog.py
# 或
bash scripts/build_catalog.sh
```

仍可使用旧字典仓库生成的 `akshare_dictionary.json`：

```bash
python scripts/build_catalog.py --src <字典仓库>/output/akshare_dictionary.json
```

可选标志：`--keep-status` 无条件继承全部实测状态；`--no-inherit` 全部标 untested。

重建后做离线漂移检查，再对需要更新的接口现场复测：

```bash
python scripts/drift.py
python scripts/retest.py --status untested
```

单个接口的现场复测仍用 `bash scripts/check.sh <接口名>`。

## 声明

- 数据来自东方财富、新浪财经等公开接口，仅供学习研究，不构成投资建议；请遵守数据源的 使用条款，注意请求频率。
- `status` 是构建当天（`meta.generated_at`）的快照，不代表永久可用性；写代码前可用 `check.sh` 复测。
- 定时烟测只能发现代表性数据源故障，不能保证所有接口、字段、延迟或交易级实时性；调用方仍应校验返回时间戳和数据形状。烟测结论写在每次 Actions 运行的 Job Summary 里，JSON 报告在 artifact `akshare-smoke-report`。

## License

[MIT](LICENSE)
