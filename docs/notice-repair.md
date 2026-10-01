# 公告失败项定向重试

首轮 `notice-translate` 完成后，可用 `notice_repair.py` 对同一份审阅清单、来源和日志中的剩余项重试。不要与正在写同一日志的任务并行运行。

```sh
python notice_repair.py --source /path/to/source.json \
  --translations /path/to/zh-Hans.json --ui /path/to/ui.json \
  --log-file /path/to/results.jsonl --review-list /path/to/review-list.json \
  --workers 50
```

独立包装器在发送前保护数字、日期格式、URL、富文本标签、占位符及原始换行。模型必须保持标记顺序及数量；还原后再执行原有数字序列、标签和换行校验，禁止靠补数字或放宽校验修复。新增阿拉伯数字同样会被拒绝。碰到原文本身含保留标记前缀时停止该项，不猜测替换。

组件复用既有公告流程：保留来源 SHA 检查和 page/source 映射，不跨页合并，不覆盖已有译文，不再发送已验证保存的成功项。日志只保存还原后的译文，后续仍可由普通命令读取。认证方式和词典沿用 `.env` 与现有 glossary 配置；可用独立 `--prompt` 调整重试提示词。

## 音乐制作人员姓名保留

批量翻译任务退出后、重试之前，运行 `notice_credit_policy.py`。它只识别以 `作詞`、`作曲`、`編曲` 或由 `・` 连接的这些字段开头、随后接冒号的单行。只转换字段标签，冒号后全部内容逐字保留。叙述中的“作曲”等词不匹配，也不做姓名子串替换。

```sh
python notice_credit_policy.py --source /path/to/source.json \
  --translations /path/to/zh-Hans.json --initial-translations /path/to/initial-zh-Hans.json \
  --log-file /path/to/results.jsonl --report /path/to/credit-policy-report.json
```

`initial-zh-Hans.json` 必须是在本轮批量翻译前保存的原译文快照：已有内容不会被覆盖，且发生变动会停止。新机器稿的修正同时写入 JSON，并在同一 page/source 日志尾部追加带策略名称、源文 SHA 和 `names_verbatim` 的最终记录，使断点续跑读取修正后的值。报告列出覆盖数量及每项处理来源。重复执行不会重复添加相同策略记录。所有结果仍需通过数字、标签和换行校验；不要与写同一日志的 API 进程同时运行。

若用户已要求旧公告中的音乐制作人员也统一保留原名，可加 `--restore-initial-credit-names`。该选项仅对严格匹配的既有 credit 行恢复冒号后的原串，保留既有中文标签和冒号，并在报告备份修复前值；其余已有译文仍受保护。没有明确冒号的旧译文会停止供审查，不猜测姓名边界。
