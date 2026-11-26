# 语音、照片和文件

[← 回到 README](../README.md)

主模型（比如 deepseek-v4-pro）看不了图、听不了语音。你在 Telegram 里发来的语音和照片照常放进主目录的
`inbox/<日期>/`，另外：

- **语音**（按住说话的语音、音频文件）交给语音识别接口转成文字，Agent 看到的消息里带着 `[语音转文字] ……`。
  接口是 OpenAI 兼容的 `/audio/transcriptions`，在 `.env` 里配：

  ```bash
  SMRA_STT_UPSTREAM=https://api.siliconflow.cn/v1     # 不填、只填密钥时用 OpenAI
  SMRA_STT_API_KEY=...
  SMRA_STT_MODEL=FunAudioLLM/SenseVoiceSmall          # OpenAI 用 whisper-1（默认）
  ```

  没配的话 Agent 只看到附件，旁边注明转不了文字。语音会发给这个识别服务。
- **照片**（以及作为文件发来的 jpg/png/webp/gif）交给能看图的模型写一段说明（图里的文字逐字转录），
  默认用注入检测的模型（deepseek 预设是 deepseek-flash），走同一个提供方和密钥。主模型看不了图、检测模型能看图时默认打开；
  `SMRA_DESCRIBE_IMAGES=0` 关掉，`SMRA_VISION_MODEL` 换模型。

结果写回收件箱里那条消息，重试时不会再调一次。超过大小（语音 20MB、图片 5MB）或者调用失败，消息照常交给 Agent，附件旁注明原因。
这两项调用不计入运行的 token 预算。

**Telegram 收发文件的规矩**。收：文档、照片（只取最大的那个尺寸）、语音、音频、视频、圆形视频消息和 GIF 都收，
贴纸、位置之类没有能交给 Agent 的内容，不理。机器人只能下载 20 MB 以内的文件，超过的不去下，消息里注明"超过 20 MB，没收到"；
一个附件下载失败不影响同一条消息里的别的附件。文件名是对方给的，存盘时只留文件名本身、怪字符换成 `_`。
不是你那个 chat id 发来的消息，附件根本不下载。发：Agent 发的文件上限 20 MB（Telegram 允许 50 MB），
中文文件名原样显示；说明超过 1024 字放不进文件的说明栏，先整段发文字、再发不带说明的文件。
