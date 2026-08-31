---
name: playlist-analysis
description: Use when the user provides a music playlist link (网易云 music.163.com / QQ音乐 y.qq.com / 酷狗 kugou.com / 酷我 kuwo.cn) and wants a multi-dimensional playlist analysis report in Markdown and HTML (曲风/情绪/年代/歌手/语言/热度/专辑/集中度/合作/词云/雷达图). Also trigger on 歌单分析、分析歌单、歌单报告、playlist report.
metadata:
  openclaw:
    emoji: "🎧"
    requires:
      python_modules: ["requests", "jinja2"]
---

# 歌单深度分析（Playlist Analysis）

抓取主流音乐平台歌单 → 脚本做**浅层统计**，AI 主代理派发**子代理**做**语义分析**（曲风/情绪/类型）→ 输出 `report.md` + `report.html`（现代极简、高动效）+ `analysis.json`。

## 核心分工原则

**脚本只做"算得准"的事，AI 只做"需要理解"的事。**

| 层 | 负责内容 | 方式 |
|---|---|---|
| 脚本（浅层） | 抓取、去重、切块、时长/年代/语言/歌手频次/专辑/HHI/排序/热度/词云/合作网络边/歌词 | 纯计算 |
| AI 子代理（语义） | 逐首标注 **曲风 / 情绪 / 类型 / 场景**（认识卫兰、Sabrina、初音未来等艺人） | LLM 判断 |
| AI 主代理（综合） | 汇总子代理结果 → 曲风解读、画像、结论、洞察、AI 词云 | LLM 综合 |
| 脚本（渲染） | 用 AI 结果渲染 md + html（黑胶杂志风） | 模板 |

> 不要试图用关键词表猜曲风——96% 的歌都会落入"流行/其他"，等于没分析。曲风/情绪必须交给 LLM。

## 工作流（两阶段）

### 阶段 1：数据管道（脚本）

```bash
# 可选加 --lyrics 抓网易云歌词（每首截断 200 字，供子代理判情绪/主题）
python3 analyze_playlist.py "<歌单链接>" -o <输出目录> --data-only
```

产出：
- `<out>/playlist.json` — 全量歌曲（每首含全局 `index`、title、artists、album、duration_ms、year、popularity、url、可选 lyric）+ 歌单元信息
- `<out>/chunks/chunk_NNN.json` — 切块（每块 ≤ **300 首**），供子代理逐个分析
- `<out>/stats.json` — 浅层统计（不含曲风/情绪等语义字段）
- `<out>/artists.json` — **歌手清单**（频次/代表曲目/年份区间，供 P0-1 先聚合标注歌手风格）
- `<out>/collab_network.json` — 歌手×歌手合作边（P1-6 合作网络图）
- `<out>/benchmark.json` — 内置对比基准（P2-10 洞察对比）

### 阶段 2：AI 语义分析

主代理按以下流程执行：

1. **读 `stats.json`**，掌握整体（体量、语言、年代、歌手频次、新鲜度、排序逻辑）。
2. **歌手聚合标注（P0-1，降低成本）**：读 `artists.json` 的 Top 歌手（出现 ≥3 次），先派一个子代理为**每个知名歌手判定主风格**（写 `<out>/ai/artists.json`），供各块子代理复用——避免对同一歌手的每首歌重复推断。
3. **逐块派发子代理（并行）**：每块一个子代理，读 `chunks/chunk_NNN.json` + 歌手风格映射，对块内每首歌标注（见协议），写回 `<out>/ai/chunk_NNN.json`。
4. **交叉抽检（P0-2）**：派一个独立子代理随机抽 5% 歌曲复查 genre 一致性；不一致率 >10% 则对应块重标。
5. **汇总（固化脚本）**：运行 `python3 merge_ai.py <输出目录>`，自动合并各块标注 → 计算分布/场景/冗余度/曲风×年代/合作网络 → 生成 `ai_analysis.json` 骨架。
6. **主代理补充解读**：在 `ai_analysis.json` 中写入 `genre_overview` / `mood_overview` / `profile` / `summary` / `insights` / `cloud`（AI 语义词云），并参考 `benchmark.json` 写对比洞察。

### 阶段 3：渲染（脚本）

```bash
python3 analyze_playlist.py "<歌单链接>" -o <输出目录> --ai-result <out>/ai_analysis.json
```

脚本复用已导出的 `playlist.json`（保证 index 与 AI 标注一致），用 `ai_analysis.json` 覆盖语义字段后渲染三件套。

## 数据协议

### 子代理输入 `chunks/chunk_NNN.json`

```json
{
  "chunk": 0,
  "songs": [
    {"index": 5, "title": "고쳐주세요", "artists": ["脸红的思春期"], "album": "Red Diary Page.1",
     "duration_ms": 186000, "year": 2017, "popularity": 75, "url": "..."}
  ]
}
```

### 子代理输出 `ai/chunk_NNN.json`（**必须覆盖块内全部歌曲**）

```json
{
  "annotations": [
    {"index": 5, "genre": "韩语独立摇滚", "type": "录音室版",
     "mood": "治愈温暖", "valence": 0.75, "energy": 0.4,
     "scene": "通勤放松"}
  ],
  "notes": "本块观察：粤语情歌与欧美流行交替出现"
}
```

- `genre`：曲风名（中文），可参考这些大类，但允许更细：流行/欧美流行/粤语流行/华语流行/韩语流行/日语流行/R&B/嘻哈说唱/摇滚/民谣/电子/舞曲/爵士/灵魂乐/古风/二次元/Vocaloid/游戏原声/影视原声/纯音乐…
- `mood`：情绪标签（中性/治愈温暖/燃热血/伤感/甜恋/深夜静谧/动感/轻快愉悦，可另加）
- `valence`：0~1 悲观→乐观；`energy`：0~1 静→动
- `scene`：使用场景（通勤放松/深夜独处/运动健身/约会路上/睡前助眠/派对氛围/雨天发呆/日常聆听，可另加）
- `type`：录音室版/翻唱/现场版/混音版/原声带/纯音乐
- 判断依据**必须是歌手+专辑+歌名**（LLM 对知名艺人有曲风知识），不要只抠歌名字面关键词。若歌曲带 `lyric` 字段（`--lyrics` 启用时抓取），可结合歌词判断情绪/主题。

### 主代理汇总 `ai_analysis.json`

主代理先运行固化脚本生成骨架，再补充解读：

```bash
python3 scripts/merge_ai.py <输出目录>
# 生成 ai_analysis.json 骨架（genres/types/moods/scenes/redundancy/genre_x_year/collab_network）
```

骨架示例：
```json
{
  "genres": {
    "rows": [{"name": "粤语流行", "count": 320, "pct": 10.2}, {"name": "欧美流行", "count": 280, "pct": 8.9}],
    "diversity": 0.71
  },
  "types": {"rows": [{"name": "录音室版", "count": 3050, "pct": 97.4}]},
  "moods": {
    "rows": [{"name": "中性", "count": 2000, "pct": 63.9}, {"name": "伤感", "count": 200, "pct": 6.4}],
    "valence_raw": [0.5, 0.75, 0.2],
    "energy_raw": [0.5, 0.4, 0.6],
    "valence_avg": 0.53,
    "energy_avg": 0.51
  },
  "scenes": {"rows": [{"name": "通勤放松", "count": 900, "pct": 28.7}]},
  "redundancy": {"value": 0.57, "level": "中"},
  "genre_x_year": {"decades": [2000, 2010, 2020], "rows": [{"genre": "粤语流行", "decade": 2020, "count": 500}]},
  "collab_network": {"nodes": [{"name": "卫兰", "weight": 12}], "edges": [{"a": "卫兰", "b": "Kiri T", "count": 3}]}
}
```

主代理在此骨架基础上补充（**不要重建骨架，只追加/覆盖**）：

```json
{
  "genre_overview": "一段对曲风构成的解读（为什么这样分布、听感如何）",
  "mood_overview": "一段对情绪基调的解读",
  "profile": ["欧美向", "新歌追更系", "高热度流行向"],
  "summary": "一句话结论",
  "insights": ["洞察1", "洞察2", "洞察3"],
  "cloud": [{"word": "粤语情歌", "size": 38}]
}
```

注意：
- `valence_raw` / `energy_raw` 必须**按歌曲原始顺序**（即 index 0..N-1），供情绪曲线 SVG 使用；缺失时补 0.5。
- `cloud` 为 **AI 语义词云**（替代脚本词频，语义化关键词如"粤语情歌/都市爱情"），每项 `word` + `size`(13~35)。
- 参考 `benchmark.json` 写对比洞察（如"平均热度 84，高于平台一般水平 60"）。
- 若某字段写不出就省略，脚本会用默认值兜底，**不要编造**。

## 子代理派发要点

- 每块一个子代理，**并行**派发；块数即 ceil(总曲目 / 300)。
- 子代理必须**逐首覆盖**块内所有歌（`index` 全集），漏一首算失败。
- 若某个子代理失败/超时，重试一次；仍失败则标记该块缺标注，汇总时用 0.5/中性 兜底并在报告注明。
- 汇总已固化进 `scripts/merge_ai.py`，`pct`/`diversity`/`valence_avg`/`redundancy`/`genre_x_year` 由它计算。

## 小歌单捷径

曲目 ≤ 300 时，只有 1 块：主代理可直接自己完成语义标注（不必真派子代理），其余流程不变。

## 分析维度（最终报告）

- **一句话结论 + 口味画像**：AI 综合生成（画像标签如"欧美向 / 新歌追更系 / 高热度流行向"）。
- **基本信息**：名称、创建者、播放量、标签、简介、创建时间。
- **时长**：总时长、平均、最短/最长、分布直方图（脚本计算）。
- **年代**：年份跨度、年代分布、逐年折线图（网易云有年份数据）。
- **曲风 × 年代**：交叉热力图（merge_ai 计算，AI 标注重算）。
- **歌手**：Top 歌手（首席位 vs 总出镜）、歌手代际（经典/中生代/新生代/新锐）。
- **歌手合作网络**：歌手×歌手共现力导向图（脚本导边，merge_ai 出节点）。
- **曲风**：**AI 标注** + AI 解读 + 曲风多样性。
- **歌曲类型**：AI 标注（录音室/翻唱/现场/混音/原声带）。
- **使用场景**：AI 标注（通勤/深夜/运动/约会/睡前…）。
- **风格冗余度**：歌单内部"听起来是否重复"指数（merge_ai 计算）。
- **情绪**：**AI 标注** + AI 解读 + 情绪-能量曲线图。
- **语言**：字符集启发式（脚本计算）。
- **热度**：平均热度、区间分布（仅网易云 `pop`）。
- **专辑**：专辑数、Top 专辑。
- **集中度 HHI**：歌手/专辑/语言/年代（脚本）；曲风用 AI 标注重算。
- **排序逻辑**：位置 vs 年份/热度斯皮尔曼相关（脚本）。
- **合作检测**：feat./&/× 特征识别 + 高频合作对象（脚本）。
- **词云**：**AI 语义词云**（主代理生成，替代脚本词频）。
- **风格雷达图**：六维画像（歌手/专辑/语言/曲风/年代/情绪能量）。
- **对比基准**：与内置基准（热歌榜/平台一般水平）对比的洞察。
- **精选 Top 30**：**综合推荐**（merge_ai 计算）——热度40% + 年份新近度15% + 合作5% 打分，贪心多样化（每主要曲风至少1首、同歌手限2首、情绪均衡），非简单取前 30。
- **新歌/旧歌**：近五年发行占比 vs 发行超十年占比（两个独立指标，可同时高），替代旧"新鲜指数"。

## 脚本位置

- `scripts/analyze_playlist.py` — 抓取 + 浅层统计 + 切块 + 渲染（`--data-only` / `--ai-result` / `--lyrics`）
- `scripts/merge_ai.py` — **主代理汇总固化脚本**（合并子代理标注 → ai_analysis.json 骨架）
- `templates/report_template.html.j2` — HTML 报告模板（Jinja2，黑胶杂志风）

## HTML 报告设计

自包含单文件，离线可打开。**黑胶唱片 × 编辑杂志**风格（去 AI 味）：
- 暖纸面底 + 墨黑文字 + 单一黑胶橙强调色，衬线标题（Georgia/宋体）
- 黑胶唱片盘装饰（hover 旋转）、极淡纸纹点阵
- 克制动效：入场淡入、数字滚动、条形生长、SVG 描边/热力图渐显（无光斑/磁吸/彩虹/噪点/玻璃拟态）
- 新板块：AI 解读引用、曲风×年代热力图（悬停看数值）、歌手合作网络力导向图（悬停高亮合作边）、使用场景、风格冗余度、AI 词云
- `prefers-reduced-motion` 降级

## 支持的链接格式

| 平台 | 链接示例 | ID 提取 |
|---|---|---|
| 网易云 | `https://music.163.com/#/playlist?id=xxx`、`https://music.163.com/playlist/xxx` | `playlist?id=(\d+)` / `playlist/(\d+)` |
| QQ音乐 | `https://y.qq.com/n/ryqq/playlist/xxx`、`https://i.y.qq.com/n2/m/share/details/taoge.html?id=xxx` | `playlist/(\d+)` / `disstid=(\d+)` |
| 酷狗 | `https://www.kugou.com/yy/special/single/xxx.html` | `special/single/(\d+)` |
| 酷我 | `https://www.kuwo.cn/playlist_detail/xxx` | `playlist_detail/(\d+)` |

## 数据可用性说明

| 维度 | 网易云 | QQ | 酷狗 | 酷我 |
|---|---|---|---|---|
| 年份 | ✅ publishTime | ❌ | ❌ | ❌ |
| 热度 pop | ✅ | ❌ | ❌ | ❌ |
| 专辑 | ✅ | ✅ | 部分 | ✅ |
| 标签 | ✅ | ✅ | ❌ | ❌ |

缺少数据的维度在报告中如实标注，不编造。网易云 >1000 首歌单通过 trackIds 分批拉全（无 1000 上限）。

## 已知限制

- **酷我（kuwo）反爬较严**：常返回 "The request is illegal"，脚本内置 3 组降级尝试，失败时明确提示建议换平台。
- **酷狗（kugou）m 站为 HTML**：脚本用正则解析，改版会回退旧版 JSON 接口。

## 常见问题

- **接口返回空/报错**：平台接口偶发反爬或改版，重试一次；仍失败如实告知用户，建议换平台或人工提供曲目清单。
- **曲名含 feat. 误判**：合作检测基于歌名文本，少量误判可接受。
- **曲风/情绪来源**：由 AI 子代理基于歌手/专辑/歌名判断，非平台官方数据，属"推测倾向"；脚本只做浅层统计。