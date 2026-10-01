# 术语提取与校验命令

所有报告均为候选，不会自动修改词典。接口配置见 [README](../README.md)。

## MasterDB 二次校对

`term_audit.py` 可离线检查专名偏好缺失、同文异译和制作人员署名，保留每项的表／记录／字段或剧情脚本／说话人上下文：

```sh
python3 term_audit.py --master-orig "$MASTER_ORIG" --master-zh "$MASTER_ZH" \
  --story-csv "$CSV_DIR" --output data/review/terms.json
```

可用 `--policy` 指定带分类、来源和作用范围的术语规则。报告是审阅候选，不会自动替换译文，也不调用 API。

### 发现新专名

先从明确的 MasterDB 实体字段与剧情说话人提取结构化候选，不调用模型：

```sh
python3 extract_term_candidates.py --master-orig <MasterDB原文目录> \
  --story-csv <剧情CSV目录> --output <本地候选报告.json>
```

报告按原文和类别聚合，保留 Master 表／记录／字段或剧情脚本／行号／说话人作用域、上下文样例和词典命中情况。人物、组合、歌曲、活动／剧情标题、服装等使用明确的实体字段；音乐制作人员署名、用户名变量和普通描述不会被当作结构化专名。默认使用本项目 `glossaries/` 内的人名和术语词典，也可传入 `--name-glossary`、`--term-glossary`。正文里的新专名需继续做语义提取。

正文语义提取使用外部 OpenAI 兼容模型，分计划、断点运行和审阅三步。`plan` 只整理静态原文、同一脚本邻句和说话人，不请求 API；`extract` 才发送原文；`review` 重新校验模型证据，不直接修改词典或译文。可以先按剧情类别与 Master 表各取少量任务抽样：

不指定 `--script-prefix` 或 `--master-table` 时，`plan` 默认纳入全部含日文的剧情正文，包括不足 8 字的短句；Master 纳入 19 张叙事／人物相关表，并从 Gacha、Emblem、ShowcaseToy 等额外实体表只取 `name/title/fullName` 字段。规则密集表仍在计划首行逐表报告排除量。普通剧情最多 16 行，短剧情单独最多 8 行，独立 Master 实体同表同规范字段最多 8 行，每批原文上限 3000 字。Master 先按同表、同字段、相同原文及记录标签去重；Message、HomeTalk、Story 保留记录边界。每行 ID、`record_labels` 和重复来源 `aliases` 均保留，审阅时再次验证原文后展开为逐记录证据。`--short-batch-rows`、`--entity-batch-rows` 与 `--no-pack-master` 可调整批次。`--profile all` 可规划所有合格字段，但应先检查范围，不直接全部发送模型。

```sh
python3 term_semantic.py plan --master-orig <MasterDB原文目录> \
  --story-csv <剧情CSV目录> --output <完整本地计划.jsonl>
python3 term_semantic.py slice --plan <完整本地计划.jsonl> \
  --story-prefix adv_main_ --story-prefix adv_event_ \
  --master-table Character --master-table CharacterGroup \
  --output <本阶段计划.jsonl>
python3 term_semantic.py extract --plan <本阶段计划.jsonl> \
  --log <未提交的结果日志.jsonl> --workers 4 --max-jobs 100
python3 term_semantic.py review --plan <本阶段计划.jsonl> \
  --log <未提交的结果日志.jsonl> --model <实际模型名> \
  --output <本地语义候选报告.json>
```

`slice` 不改变任务 ID 或缓存键，可按剧情前缀、Master 表或 `--master-mode independent|narrative` 生成阶段计划；重复执行同一 `extract` 命令会从成功结果之后继续。计划与结果日志包含原文，应放在未提交的分析目录。模型输出的空候选不算技术失败，但仍需抽查是否漏掉短人名或地点；不能用计划任务数代替实际完成数。

`repair-empty` 从已完成但空候选的短剧情／实体名称批生成至多 4 行的新任务；`repair-partial` 对严格证据校验失败的批次做同样处理。两者均保留原任务和行 ID，结果使用独立缓存键；重复运行同一修复任务不会再次请求已完成项。原文内的换行可作为候选的一部分，但候选仍须逐字存在于所引行，控制字符、过长文本及不存在的行均被拒绝。`extract` 使用有界滚动调度，同时在途最多 `min(workers, 64)` 个请求，完成后补充下一项。连续 64 项失败（包括 partial）时停止新提交，处理完已在途请求后退出；成功结果重置连续失败计数，但已触发的熔断不会恢复提交。任何失败均返回非零退出码；部分合格的批次不会被误报为完成。

`extract` 从本项目未提交的 `.env` 或环境变量读取 `OPENAI_API_BASE`、`OPENAI_MODEL`、`OPENAI_API_KEY`。提示词默认为 `prompts/term-extraction.txt`，可用 `--prompt` 指定；审阅时须使用相同提示词与模型名。缓存键绑定原文和上下文、提示词及模型名，重跑只补未成功任务。术语必须在引用行的原文中精确出现，候选保留来源和建议译名变体，需人工确认后再改词典。制作人员仅按 Master credit 字段或显式署名行排除，不全局禁用同名角色。设计参考 [KeywordGacha](https://github.com/neavo/KeywordGacha) 的实体类别、[GalTransl](https://github.com/GalTransl/GalTransl) 的分批缓存和 [LinguaGacha](https://github.com/neavo/LinguaGacha) 的证据约束；本项目使用自行编写的提示词和实现。

模型把说话人或邻句中的名称误当成本行证据时，提取器会用严格校验纠错重试；最终仍不合规的候选保存在结果日志的 `status=partial` 记录中，不计为完成，也不进入审阅候选。重新运行会继续这些任务。日志仅输出静态校验原因，不输出密钥或原始 HTTP 响应。

新生成的 `partial` 记录在 `rejected_candidates` 中单独保存最后一次尝试被拒的候选字段及校验原因，便于本地核对表记。`review` 将其列入独立的 `unverified_candidates`，不会混入通过证据校验的 `candidates` 或词典；旧日志缺少候选明细时明确标注 `candidate_details_available=false`。这些诊断不包含 HTTP 响应、headers 或 API 密钥。

语义证据校验允许标题原文中的字面 `\n`（反斜杠与 n 两个字符），仅在占位符检查时豁免这一格式。候选及来源不做换行规范化：字面 `\n`、真实换行、去掉换行后的文本必须分别逐字命中原文。真实变量／占位符、字面 `\r`／`\t` 仍拒绝，日期／纯数字过滤也保持原规则。

`term_review.py` 将候选与现有译名对照，不调用 API、不修改词典：

```sh
python3 term_review.py --review <语义候选报告.json> --review <补查候选报告.json> \
  --master-orig "$MASTER_ORIG" --master-zh "$MASTER_ZH" \
  --name-glossary glossaries/name-glossary.json \
  --term-glossary glossaries/term-glossary.json --output <本地译名对照.json>
```

仅用完整原文匹配 Master 的 `name`、`title`、`fullName` 字段，保留每个表／记录／字段来源；不拿正文片段套用整段译文。同名异译、模型建议与已有译名不一致、缺少参考的候选分别标记供审阅。重复补查的证据去重，不把重复次数视为译名投票。原样保留的名称也参与冲突检查；现有译名只是项目参考，不代表官方译名或自动批准。

`term_review_export.py` 将已对照的参考报告和原始行覆盖报告导出成可人工校对的稳定审阅包，不调用 API、不应用决定：

```sh
python3 term_review_export.py --references <译名对照.json> \
  --coverage <原始行覆盖.json> --classifications <可选原行分类.json> \
  --output-dir <本地化项目的审阅目录>
```

`queue.json` 包含参考／模型建议／原始证据及未验证行；`decisions.json` 使用稳定 review ID 记录人工决定。输入仅记录文件名与原文件 SHA，证据路径递归改为项目相对路径，避免把本机用户名带入归档。未验证行指纹包含原行、最后结果和本地分类；分类变化时已有人工决定转入 `stale_decisions`，重新审阅。未填写的 pending 模板保持 pending。缺参考、参考异译和来源覆盖缺口可以重叠，不能把三类数量相加当候选总数。本地无专名判断与成功模型批的原始行覆盖分别记录。ASCII 名称紧跟游戏字面 `\n` 时仅语义证据校验承认该换行边界，不将其他 ASCII 名称的内部片段视为独立词。

`suggest_master_kana.py` 使用 `prompts/master-kana-review.txt` 为仍含假名的译文生成建议：

```sh
python3 suggest_master_kana.py --report "$REVIEW_REPORT" \
  --orig-dir "$MASTER_ORIG" --zh-dir "$MASTER_ZH"
```

报告包含 `kana_issues` 列表，每项用 `table`、`id`、`path` 定位字段，可由本地化项目的 `audit_master_json.py` 生成。默认输入在本项目 `data/master/`，建议与断点日志写入 `data/master/working/`，可用 `--output`、`--log` 指定。该命令只生成建议，不回写审校 JSON；人名和术语使用本项目词典，也可用 `--name-glossary`、`--term-glossary` 覆盖。

