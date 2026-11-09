---
name: fund-research-db
description: 利得基金投研数据库问答：公募三表（纯债/权益/固收+）分类方法论与评分数据库查询，以及QDII、基金经理、交易能力、可转债、量化策略、业绩归因、行业投资能力、行业轮动、行业配置工具、基金公司、私募基金等扩展数据库的口径/方法论解读。
version: 1.5.0
tags: [fund, research, classification, internal]
allowed-tools: [run_script, write_script, read_file, write_file, send_file]
# 会话脚本可直接 from lib import …（run_script 把本 Skill 的 scripts/ 放进 PYTHONPATH）
python-path: [scripts]
enabled: true
---

# 利得基金研究数据库

面向财富管理专业投资者/用户：可以直接讨论分类逻辑、评分口径，按条件筛数据，给具体数字和排名。

## 不要重复取材

- **本轮之内**：读过的 `references/*.md`、查过的 `fund_db_fields` 结果都还在上下文里，直接用，不要再读、再查。
- **跨轮**：上一轮读过的文档和列清单本轮**不一定还在**（上下文只保留工具调用的简要记录）。要精确列名或口径原文时，重读**当前需要的那一份**——**不要凭记忆猜列名**，猜错只会多一轮报错。用户追问、补充口径或让你接着做时也一样，只补读这次用得上的那份，不要把 references 通读一遍。

## 交付方式

- **对比、评价、分析类**（多只基金对比、某经理 / 公司评价、组合拆解），或结果 **10～49** 条：用报告卡片交付（`publish_report`，规则见系统提示词「报告卡片」）。报告自带 PDF / Excel 导出，不要再 `write_file` + `send_file` 另发一份。
- 结果 **< 10** 条的简单名单或单项查询：在对话里用 Markdown 表格 / 要点完整列出。
- 结果 **≥ 50** 条（以返回的 `matched` / 实际行数为准）：说明总条数与筛选口径，简要列前 5～10 条（要点或短表），再问用户要不要发 Excel（完整结果）。用户表态前停在这一步，不要一次贴满表，也不要直接发文件：
  - 明确要文件（「发 excel」「导出」「发附件」等）→ `write_file` / 脚本写出 → `send_file`；
  - 明确不要文件（「不要文件」「不用 excel」「对话里看」「直接表格」等）→ 用 Markdown 表格在对话里分批列出（每次 20～30 行并提示还可继续，避免单条消息过长）。
- 用户一开始就明确要文件：直接落盘 → `send_file`，不必先摘要再问。
- 「不要列表格 / 用表格看」这类说法按对话内 Markdown 展示理解，**不等于**要文件。

## 两类问题，两条路径

**A. 方法论/口径/算法问题**（分类怎么定、标签含义、评分公式、**代理变量/择时效果怎么测、风险偏好用什么指标**）→ **先** `read_file` 读 reference，原文通常已有定义与公式。不要先查库排序，也不要去翻 sqlite 库文件；需要举例验证时再 `fund_db_list` + `order_by`。

| 问题涉及 | 读哪个文件 |
|---|---|
| 分类范围、一二三级分类关系、总览、**数据库家族全景** | `<skill:fund-research-db>/references/methodology-概述.md` |
| 主动纯债基金的分类规则/评分/标签 | `<skill:fund-research-db>/references/methodology-纯债.md` |
| 主动权益：分类/风格因子/业绩归因/**T-M择时选股**/市场适应性 | `<skill:fund-research-db>/references/methodology-权益.md` |
| 主动固收+：风险特征/周胜率/评分 | `<skill:fund-research-db>/references/methodology-固收-plus.md` |
| 机构常见问答（含择时区间等） | `<skill:fund-research-db>/references/faq.md`（优先查） |
| 列名↔定义对照 | `<skill:fund-research-db>/references/field-dictionary.md` |
| QDII 基金分类/投资地区/评分 | `<skill:fund-research-db>/references/methodology-QDII.md` |
| 基金经理能力圈/偏好/擅长领域 | `<skill:fund-research-db>/references/methodology-基金经理.md` |
| 基金交易能力/换手率评价 | `<skill:fund-research-db>/references/methodology-交易能力.md` |
| 可转债投资能力/转债归因 | `<skill:fund-research-db>/references/methodology-可转债投资.md` |
| 量化策略/指增/量化选股分类 | `<skill:fund-research-db>/references/methodology-量化策略.md` |
| 业绩归因评分/Brinson 四维分解 | `<skill:fund-research-db>/references/methodology-业绩归因评分.md` |
| 基金经理行业投资能力/主题能力 | `<skill:fund-research-db>/references/methodology-行业投资能力.md` |
| 基金经理行业轮动/择时/配置 | `<skill:fund-research-db>/references/methodology-行业轮动.md` |
| 行业配置工具/持仓明细(中信/申万) | `<skill:fund-research-db>/references/methodology-行业配置工具.md` |
| 基金公司评估/背景/规模/资质 | `<skill:fund-research-db>/references/methodology-基金公司.md` |
| 私募基金/私募策略/管理人 | `<skill:fund-research-db>/references/methodology-私募基金.md` |
| 典型投研问题示例/提问模板 | `<skill:fund-research-db>/references/提问指南.md` |
| **写 `fund_db_list` 查询或跨表脚本前**：19 个实体的主键、Join 键、核心筛选/排序字段、评分字段、跨表路径 | `<skill:fund-research-db>/references/schema-catalog.md` |

与「权益风险偏好代理 / 择时效果检测」直接相关的库内字段（先读方法论再决定是否用 list 验证）：
- **择时/选股**：`T-M模型择时能力`、`T-M模型选股能力`（公式与区间见 methodology-权益 + faq #11）
- **风险偏好/弹性代理（可讨论组合）**：`Beta因子`、`波动率因子`、`近1年年化波动率(%)`、`近1年最大回撤(%)`、`权益占净值比(%)`、`估值属性`/`市值属性`、固收+ 的 `风险特征`
- **市场适应性（情景）**：`适配情景` / `不适配情景`

**B. 具体数据查询** → 一次工具调用能做完就用 `fund_db_*`，参数按工具定义直接传（`entity` 填表名），不要再经 `run_script` 调 `query.py`。要合并两张及以上表、组内排名或分档、算库里没有的指标、取全量（超过 200 行）再加工、出 Excel 或图时才写脚本（见下文「写脚本做二次加工」）。

典型问法和标准做法：

- 「工银双利是不是二级债基」→ `fund_db_get(entity="active_fixed_income_plus_funds", keyword="工银双利")`
- 「市场上做利率债的有多少只」→ `fund_db_list(entity="active_bond_funds", filters=[{"field":"利得三级分类","op":"eq","value":"利率债基"}], limit=10)`，总数看 `matched`
- 「资源周期平均持仓 40% 以上的基金」→ `fund_db_list(entity="manager_industry_capabilities", filters=[{"field":"资源周期_持仓比例_平均值(%)","op":"gte","value":40}], order_by={"field":"资源周期_持仓比例_平均值(%)","direction":"desc"})`
- 「量化基金里有哪些小微盘分类」→ `fund_db_values(entity="quant_equity_funds", field="利得量化策略二级分类")`
- 「这只基金在同类里各项指标排第几」→ 写脚本：`query_df` 取同类全量，`rank_of` 算名次

- 九成「这只基金什么风格/特点」的问题，直接读 `fund_db_get` 返回的「一句话基金画像」（写好的自然语言摘要）；涉及具体数字/排名才读单独的数值字段。
- **综合评分不可跨表比较**：各表评分构成与基准池不同。

### `fund_db_get` 的匹配结果

- `exact_code` / `unique_name` 对上一只；`fuzzy_name` 是近似对上（`matched_name` 是库里简称），直接用，回答里注明用的是哪只。
- `ambiguous`：从 `candidates` 挑出用户说的那只，用代码再查；拿不准就问用户。
- `none`：`similar` 只是名字相近的，**不要当成用户问的那只**；也可能不在这张表，换 `entity` 再查；不要拿关键字大范围 `fund_db_list` 碰运气。

### 翻页与 `matched`

- `matched` 是**满足 filters 的总条数**，不是全表行数。它比 `len(rows)` 大只说明**还有下一页**，不代表筛选没生效；要确认筛选，看 `rows` 里该字段的值，不要因为 `matched` 大就反复重查（曾有一轮为此连查三次，最后自己推翻了自己）。
- 要逐页看就照 `next_offset` 重复同一个查询，直到 `truncated` 为 false；要全量再加工就写脚本用 `query_df` / `query_all`（自动翻页）。不要「加大 limit」硬取（上限 200），也不要自己构造 `字段 > 上页最后一个值` 的游标。

## 禁止的弯路

- 数据只通过 `fund_db_*` 工具或脚本里的 `lib` 访问：**不要**满盘找数据库文件，**不要**在脚本里 `import sqlite3` 直连 `fund_research.sqlite3`。查询报错是口径问题，不是工具不够用；直连会跳过字段校验与评分口径约束，算出来的数字不可信。
- **不要读本 Skill 的实现源码**（`scripts/query.py`、`scripts/backend.py`、`scripts/build_cache.py`、`scripts/entities.yaml`），它们是实现细节，读了也答不了业务问题：有哪些列看 `references/schema-catalog.md` 或 `fund_db_fields`，列是什么意思看 `references/field-dictionary.md`，传错了照报错改（见下表）。

### 查询报错怎么办（按 `error.code` 对号入座）

失败返回 `{"ok": false, "error": {"code": "...", "message": "...", "suggestions": [...]}}`，`message` 里已经写了怎么改。

| `error.code` | 正确动作 | 不要做 |
|---|---|---|
| `FIELD_NOT_FOUND` | 换成 `suggestions` 里最接近的列名重查；都不对再 `fund_db_fields` | 读 `entities.yaml` / `backend.py` 猜实现 |
| `FIELD_NOT_FILTERABLE` / `FIELD_NOT_SORTABLE` | 换成报错里列出的可用字段；确实要按这一列筛，就先用别的条件取数，再在脚本里过滤 | 换成直连 sqlite |
| `OP_NOT_ALLOWED` | 照报错给出的允许值改 | 换成直连 sqlite |
| `DATA_MISSING` | 数据源未就绪，换表/换字段都没用，**不要再试别的表**；直接告诉用户「投研数据暂未就绪，请联系管理员更新后再试」，可附一句业务口径供其确认 | 逐张表重试；把错误码、文件名、目录路径或「数据管理页上传发布」等内部流程写给用户 |
| （成功但）结果条数不对预期 | 调 `filters` 阈值，说明口径后重查 | 自己写 SQL 重算一遍 |

### 库里缺口径时（不要自己造口径）

问题仍属投研筛选/评价、库内却没有对应口径（例如「基金公司排名前 70」，库内并无公司排名列）时：
1. 先 `fund_db_fields` 确认确实没有；
2. **明说库内没有该口径**，并给出可落地的替代（如「按公司在三张表内的合计规模排名近似」）；
3. **让用户确认口径再算**——同一个说法可能指规模前 70 名，也可能指前 70%，口径不同结果差很远；
4. 用户确认后再执行，并在交付时把实际口径写进结果说明里。

禁止未经确认就自行定义口径出数——那样给出的排名无从复核。

### 写脚本做二次加工

只在一次工具调用做不完时写（跨表合并、组内排名分档、算新指标、取全量再加工、导出 Excel、出图），且只加工查询**输出**。`scripts/lib.py` 已在 `PYTHONPATH` 里，**直接 `from lib import …`**：不用拼 `sys.path`，不要 subprocess 拉 `query.py`，也不要自己写分页循环。`lib` 的函数既认 `entity`（表名）也认 `category`（简写或别名，如「QDII」「基金经理」「私募」「转债」）。

```python
from lib import query_df, rank_of, show

# 同类全量（内部自动翻页，无重无漏）
peers = query_df({
    "entity": "active_fixed_income_plus_funds",
    "filters": [{"field": "利得二级分类", "op": "eq", "value": "二级债基"}],
    "select": ["基金代码", "基金简称", "综合评分", "近1年收益(%)", "基金规模(合计)(亿元)"],
})
show(rank_of(peers, "基金代码", "485111.OF", ["综合评分", "近1年收益(%)", "基金规模(合计)(亿元)"]))
show(peers.sort_values("综合评分", ascending=False), limit=10)
```

| 函数 | 用途 |
|---|---|
| `query_df(payload, max_rows=None)` | 自动翻页取满，返回 `pandas.DataFrame`（列顺序同 `select`） |
| `query_all(payload, max_rows=None)` | 同上，返回 `list[dict]` |
| `query(payload)` | 单页，返回含 `matched` / `rows` / `next_offset` 的原始结果 |
| `rank_of(df, key_col, key, value_cols, ascending=False)` | 某个对象在 `df` 里各指标的名次、有效数和前百分比；回撤、费率这类越小越好的传 `ascending=True` |
| `show(data, columns=None, limit=30, digits=2)` | 按 Markdown 表格打印前几行。**打印结果一律用它**，不要整行 `print(dict)`：输出超长会被外置，还得分页再读 |
| `get_fund(category, keyword)` | 单只基金完整档案 |
| `fields(category)` | 真实列名清单 |
| `values(category, field, filters=None, limit=50)` | 某个字段的取值与条数（文本列）或分布（数值列），同 `fund_db_values` |
| `QueryError` | 字段名/操作符非法时抛出，消息就是校验器原文（含最接近的列名或允许值） |

出图就在同一个脚本里取数后用 matplotlib 画（绘图约定见工具约定里的 run_script）。落盘用环境变量定位，**不要写死绝对路径**：`CSAGENT_OUTBOUND_DIR` 是交付物位置（写好后才谈 `send_file`）。脚本以项目根为工作目录。

### 评分演示怎么做（问「这个分是怎么算出来的」时）

**单项评分是库内已有的列**（`收益评分`/`风控评分`或`波动评分`/`回撤评分`/`投资效率评分`/`投资经验评分`或`周胜率评分`），直接 select 出来用，**不要试图从原始指标逐位复算**。公式本身清楚（组内排名标准化，见 `faq.md` #7 口径注解），但要用到「参与评分的基金池 N」，而 **N 无法从库内字段重建**——成立日期任意截止点、`风险特征=未满一年`、指标空值、按二级/三级/二级×三级/风险特征的各种分组都已实测排除。这个"差一点点"最有欺骗性：曾有一轮为此写了 6 个脚本、跑了 15 轮仍未收敛，最后只能退回直接取库内值。

正确的演示结构：
1. 一次 `get_fund` 或 `query` 取该基金的单项评分与综合评分；
2. 展示加权关系——这一层**可以精确对上**（全表实测最大误差 1.78e-15），是真正值得演示的部分：

   | 表 | 公式 |
   |---|---|
   | 纯债 | `综合 = 0.20 × (收益+风控+回撤+投资效率+投资经验)` |
   | 权益 | `综合 = 0.25 × (收益+风控+回撤+投资效率)`　**4 项** |
   | 固收+ | `综合 = 0.20 × (收益+波动+回撤+投资效率+周胜率)`　注意是**波动评分** |
3. 解释单项评分的来历：组内排名标准化，见 `faq.md` #7 口径注解；
4. 要给排名感受时，用 `query_df` 拉组内数据、`rank_of` 算各指标上的**名次**（名次可靠），但不要用名次反推分数。

## 常见字段枚举取值速查

- **基金规模评价**：偏小 / 中等 / 偏大
- **机构持有风格**（纯债、固收+ 才有此字段，权益表没有）：低 / 中等 / 高
- **风险特征**（仅固收+）：低波动 / 中波动 / 高波动 / 未满一年
- **行业投资集中度风格**（权益、固收+ 两表阈值一致）：分散(<60%) / 集中(≥60%)

完整枚举/阈值表见 `field-dictionary.md`，以该文件为准。

## 数据新鲜度

19 张表来自 12 个投研源文件，由管理员在「数据管理」页面上传发布（存服务器数据目录 `FUND_RESEARCH_DATA_DIR`，不在代码仓库里）；发布时只重建对应的表并原子切换，查询不会现场重建。用户问「数据是哪一期的」时，以 `fund_db_fields` 返回或表中的数据日期字段为准。

## 边界

- 内部研究数据：对话像是要转发给客户或生成对客文案时，提醒一句这是内部数据，不直接转发。
- 不做换基推荐；不承诺过往业绩代表未来表现。
- 评分/排名是同类基金内部的相对排名，不是绝对收益承诺，避免「最好」「必买」这类绝对化表述。
- 查不到的基金如实说明「未在本数据库中」，不臆造分类结果。
