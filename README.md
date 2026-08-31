# 歌单深度分析（Playlist Analysis）🎧

抓取主流音乐平台歌单（网易云 / QQ音乐 / 酷狗 / 酷我）→ **AI 主导的多维度分析** → 输出 `report.md` + `report.html`（黑胶杂志风、高动效）+ `analysis.json`。

## 一句话简介

**脚本只做"算得准"的事，AI 只做"需要理解"的事。** 曲风 / 情绪 / 类型 / 场景这类需要语义理解的维度由 AI 子代理标注，脚本负责抓取、切块与纯计算统计。

## 核心分工

| 层 | 负责内容 | 方式 |
|---|---|---|
| 脚本（浅层） | 抓取、去重、切块、时长/年代/语言/歌手频次/HHI/排序/热度/词云/合作网络边/歌词 | 纯计算 |
| AI 子代理（语义） | 逐首标注 曲风/情绪/类型/场景（认识卫兰、Sabrina、初音未来等艺人） | LLM 判断 |
| AI 主代理（综合） | 汇总子代理结果 → 曲风解读、画像、结论、洞察、AI 词云 | LLM 综合 |
| 脚本（渲染） | 用 AI 结果渲染 md + html | 模板 |

> 不要用关键词表猜曲风——96% 的歌都会落入"流行/其他"，等于没分析。

## 工作流

```bash
# 阶段1：数据管道（可选 --lyrics 抓网易云歌词）
python3 scripts/analyze_playlist.py "<歌单链接>" -o <输出目录> --data-only

# 阶段2：AI 语义分析（主代理派发子代理逐块标注 → 汇总）
python3 scripts/merge_ai.py <输出目录>

# 阶段3：渲染
python3 scripts/analyze_playlist.py "<歌单链接>" -o <输出目录> --ai-result <输出目录>/ai_analysis.json
```

详细协议（子代理输入/输出 JSON 格式、歌手聚合、交叉抽检）见 `SKILL.md`。

## 分析维度（20+）

一句话结论 · 口味画像 · 适听人群 · 金句精选 · 基本信息 · 时长 · 年代 · 曲风×年代热力图 · 歌手/代际/影响力分级 · 冷门好歌 · 歌手合作网络 · 曲风 · 歌曲类型 · 使用场景 · 歌词主题+主题×曲风 · 风格冗余度 · 情绪曲线+叙事弧线 · 语言 · 热度 · 专辑 · 集中度HHI · 排序逻辑 · 合作检测 · AI词云 · 风格雷达 · 对比基准 · 精选Top30综合推荐（含理由） · 分享文案 · 新歌/旧歌

## 环境要求

- Python 3.9+
- 依赖：`requests`、`jinja2`

```bash
pip install requests jinja2
```

## 支持的链接格式

| 平台 | 示例 |
|---|---|
| 网易云 | `https://music.163.com/#/playlist?id=xxx` |
| QQ音乐 | `https://y.qq.com/n/ryqq/playlist/xxx` |
| 酷狗 | `https://www.kugou.com/yy/special/single/xxx.html` |
| 酷我 | `https://www.kuwo.cn/playlist_detail/xxx` |

## 文件结构

```
playlist-analysis/
├── SKILL.md                          # skill 完整文档与子代理协议
├── scripts/
│   ├── analyze_playlist.py           # 抓取 + 浅层统计 + 切块 + 渲染
│   └── merge_ai.py                   # 主代理汇总（分布/场景/冗余度/热力图/合作网络/精选推荐）
└── templates/
    └── report_template.html.j2       # HTML 报告模板（黑胶杂志风）
```

## 已知限制

- 酷我反爬较严，常返回 "The request is illegal"，脚本内置降级尝试，失败会提示换平台。
- 酷狗 m 站为 HTML，脚本用正则解析，改版会回退旧 JSON 接口。