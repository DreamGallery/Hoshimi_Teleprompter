# 翻译命令与输入格式

接口配置与词典见 [README](../README.md)。

三个 API 翻译命令 `translate`、`master-translate`、`notice-translate` 可加 `--term-policy PATH`，读取审计使用的 schema v1 政策 JSON。政策词条的 `scope` 可用 `kind/table/id/field/script/speaker` 通配模式限定；缺少对应上下文的词条不会匹配。剧情提供脚本文件名、字段 ID 和说话人；MasterDB 提供表、记录 ID 和字段；公告提供 `kind=notice`、`table=Notice`、`id=页面路径`、`field=text`。匹配词条按单条输入送进 API 请求，较具体的 scope 优先；相同范围指定不同译名会报错。命中规则时仅覆盖该行的同名平面词表偏好，未命中时仍使用原有平面偏好。政策仅指导新的 API 翻译，不改现有译文，也不做子串替换。

MasterDB 显式提供 `--term-policy` 时按表、记录 ID、字段及原文分别翻译和记录结果，允许同一原文在不同字段采用不同 scoped 词条。`master-plan --term-policy PATH` 可预览字段级待译数量；普通 `master-translate` 仍使用原文级兼容流程。字段级流程仅在当前字段原文和有效术语偏好均匹配时复用新日志；旧原文级日志只在该原文所有当前位置都没有命中的 scoped 偏好时复用。已有译文字段不会被覆盖。`Music` 表的 `composer`、`lyricist`、`arranger` 新条目直接保留原文，不发送给 API；已有译文仍需用审计报告单独审阅。

## 剧情 CSV

输入为 HoshimiToolkit 导出的 CSV 及对应原 TXT，也可从 Hoshimi-Adv 数据仓库获取。将 `CSV_DIR` 和 `ADV_TXT_DIR` 设置为相应目录后执行：

```sh
python3 main.py plan --csv-dir "$CSV_DIR" --source-dir "$ADV_TXT_DIR"
python3 main.py translate --csv-dir "$CSV_DIR" --source-dir "$ADV_TXT_DIR"
```

CSV 列为 `id,name,text,trans`。工具只填写正文与标题的 `trans` 列，保留字段 ID、占位符和字面换行标记；`name` 列提供说话人上下文。姓名翻译在后续由 HoshimiToolkit 合并 TXT 时写入。

翻译前核对 CSV 与原 TXT，重跑会保留已填写的译文。`--file`、`--prefix`、`--max-files` 可限定范围，`--max-batches-per-file` 可限制试译批次。仅生成翻译计划时可省略 `--source-dir`；实际翻译必须提供原 TXT。

## MasterDB JSON

输入格式为 `记录 ID → 字段路径 → 文本`，每张表一个 JSON。游戏完整原始表需先转换成这一文本格式。默认从本项目 `data/master/orig/` 读取，写入 `data/master/zh-Hans/`：

```sh
python3 main.py master-plan
python3 main.py master-translate
```

已有外部数据时用 `--orig-dir`、`--zh-dir` 指定原文和译文目录，`--log-file` 指定断点日志。默认日志在 `data/master/working/results.jsonl`。

## 公告网页 JSON

默认输入为 `data/notice/source.json`，译文为 `data/notice/zh-Hans.json`；可复用的界面词典放在 `data/notice/ui.json`。源文件使用 `pages → 页面路径 → {title, texts}`，其中 `texts` 为去重原文列表；译文使用 `pages → 页面路径 → 原文到译文的字典`。首次使用时将译文初始化为 `{"pages": {}}`，没有可复用界面词典时将 `ui.json` 初始化为 `{"text": {}}`。

```sh
python3 main.py notice-plan --output data/notice/review.json
python3 main.py notice-translate --review-list data/notice/review.json
```

`--source`、`--translations`、`--ui`、`--log-file` 可分别指定外部文件。审阅清单绑定源文件哈希；新增公告后需要重新生成清单。`--max-batches 1` 可用于试译。

## 界面词典

`translate_ui.py` 使用 `prompts/ui.txt` 翻译稳定 i18n 键、已审阅的原文变体或运行时 Text：

```sh
python3 translate_ui.py --source "$UI_SOURCE" --translations "$UI_TRANSLATIONS" \
  --variants-source "$UI_VARIANTS"
```

三个参数分别指向界面原文、译文和变体 JSON；默认位于本项目 `data/ui/`。没有原文变体时，变体文件填写 `{}`。已存在的译文（包括有意保留原文的条目）不会被覆盖。`--glossary` 可指定人名表，`--prompt` 可替换提示词。

加 `--variants-only` 仅翻译原文变体；加 `--text-only --text-candidates "$CANDIDATES" --text-review "$REVIEW"` 只翻译人工审阅为 `translate` 且已加入界面原文词典的 Text。审阅清单由本地化项目的运行时审阅工具生成，读取时核对源文件哈希和审阅决策；`skip`、`retain` 及已填写译文的项目不会送往模型。

