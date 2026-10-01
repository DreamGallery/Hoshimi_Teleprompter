# Hoshimi_Teleprompter

《偶像荣耀》文本翻译与术语检查工具，使用 OpenAI 兼容接口处理剧情 CSV、MasterDB JSON、界面词典、公告和法律条款，支持并发与断点续跑。

## 配置

需要 Python 3.10+，无额外 Python 依赖。复制 `.env.example` 为 `.env`，配置 `OPENAI_API_BASE`、`OPENAI_MODEL`、`OPENAI_API_KEY`，或通过环境变量提供。`.env`、生成数据和日志不会提交。

各入口提供 `--help`。输入、输出、词典和提示词均可通过参数指定；无需其他项目位于固定目录。默认使用本工具目录下的 `data/`、`adv/csv/`、`glossaries/` 和 `prompts/`。自动化调用应显式传入共享数据与词典路径。

## 使用

| 入口 | 用途与说明 |
| --- | --- |
| `main.py plan/translate` | [剧情 CSV 翻译](docs/translation-cli.md#剧情-csv) |
| `main.py master-plan/master-translate` | [MasterDB 翻译](docs/translation-cli.md#masterdb-json) |
| `main.py notice-plan/notice-translate` | [公告翻译](docs/translation-cli.md#公告网页-json) |
| `translate_ui.py` | [界面词典](docs/translation-cli.md#界面词典) |
| `legal_translate.py` | [法律条款翻译](docs/legal-translation.md) |
| `notice_repair.py` | [公告缺口修复](docs/notice-repair.md) |
| `term_audit.py`、`extract_term_candidates.py`、`term_semantic.py`、`term_review.py`、`term_review_export.py`、`suggest_master_kana.py` | [术语提取、校对与报告](docs/term-tools.md) |

`plan` 和本地审计命令不调用模型。API 命令使用 `--env-file`、`--prompt` 和并发参数调整配置；发送内容包括选定源文和词典上下文。

已有译文保留，翻译会核对源文、字段 ID、占位符及换行；法律条款还保护编号、数字、URL 和来源哈希。术语报告生成候选，不自动更新词典或译文。输入格式与作用域词典政策见[翻译命令](docs/translation-cli.md)。
