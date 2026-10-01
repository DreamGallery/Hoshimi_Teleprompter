# 条款正文翻译

源文件由 HoshimiToolkit `rule_fetch.py` 从官方匿名 `Master.Rule` 接口获取。单独维护条款词典，不进入通用 UI 或公告词典。

若匿名接口的第 4 类特商法正文为空，而游戏规则弹窗实际有正文，可保存经过核对的 `runtime-source-fallback.json`，调用 Toolkit 时加 `--runtime-fallback`。补充文件只允许第 4 类，记录完整原文、SHA-256 及 `game_runtime_capture` 来源；只有接口返回空文时采用。接口以后返回非空正文时优先采用接口数据，正文变化仍必须经过翻译源摘要检查。不要把已有非空正文自动覆盖为空文，或将整个采集日志当作条款原文。

```sh
python legal_translate.py init --source /path/to/legal/source.json \
  --translations /path/to/legal/zh-Hans.json
python legal_translate.py plan --source /path/to/legal/source.json \
  --translations /path/to/legal/zh-Hans.json
python legal_translate.py translate --source /path/to/legal/source.json \
  --translations /path/to/legal/zh-Hans.json --workers 50 \
  --output /path/to/legal/translation-report.json
python legal_translate.py compile --source /path/to/legal/source.json \
  --translations /path/to/legal/zh-Hans.json --output /path/to/legal/runtime.json
```

API 配置、OpenAI 兼容客户端和词典复用现有项目配置；翻译提示词独立存放 `prompts/legal.txt`。仅规则 1、2、3、4、5、7 的非空日文段落发送模型。类型4接口返回空不代表游戏弹窗无正文；完整来源经核验后可显式迁移并翻译。英文许可证、空正文、空行和无需翻译的行原样保留。

`source.json` 的 `rules[type]` 保存整段原文、`source_sha256`、逐行 `segments`（`id/source/line_ending`）。`zh-Hans.json` 的对应规则保存同一 SHA 和 `segments`（行 ID → 译文），可直接校对。原文 SHA 改变会停止，需审核源文差异后显式迁移，不能自动覆盖旧译文。

翻译按批并发处理，失败批再按单行尝试一次；客户端网络重试也有上限。每批成功后原子保存，可断点续跑。报告不记录密钥、请求响应体或账号信息。

编号、数字、URL 和边界空白在发送前保护。导出要求全部必译行完成，通过源 SHA、行分隔符、编号/数字/URL、占位符检查；不完整时不更新 runtime。`runtime.json` schema v1 使用 `rules[type] = {source, translation, source_sha256, translation_sha256}`，仅按完整原文匹配。任何类型的空文和类型 6 的许可证原样保留，类型4非空日文必须走完整翻译和校验。

接入构建时重新运行 compile，或逐条确认 runtime 的 source 等于 source.json 原文，两个 SHA 均为相应 UTF-8 字符串的 SHA-256；禁止只检查文件日期。游戏更新原文后，旧译文应失配并回落原文。
