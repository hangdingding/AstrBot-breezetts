# Breeze 语音回复（AstrBot 插件）

把机器人的 AI 文字回复自动转成语音消息，底层调用 [Breeze TTS](https://breezeblue.ai)。

- **全局语音回复开关，默认开启**：开启后所有回复都用语音发；
- 音色、语速、语言、单条字数上限都可在面板里配；
- 超过字数上限、命令回复、平台不支持语音时自动保持文字；
- 附带余额查询和音色搜索指令。

## 安装

**插件市场**（推荐）：AstrBot WebUI → 插件 → 安装，填仓库地址：

```
https://github.com/hangdingding/astrbot_plugin_breeze_tts
```

**手动**：

```bash
cd AstrBot/data/plugins
git clone https://github.com/hangdingding/astrbot_plugin_breeze_tts
# 回到 WebUI 插件页点重载；依赖会自动安装，也可手动 pip install -r requirements.txt
```

要求 AstrBot `>= 4.11.0`。

## 可视化页面

插件详情页里有 **Breeze 语音设置** 视图（`views/panel`）：API Key、余额、全局语音回复开关、语速、音色搜索与试听、设计音色、福利与邀请入口都在这一页里，和 DSH 版面板一致。

后端 API 由 `main.py` 的 `register_web_api` 提供（`/astrbot_plugin_breeze_tts/...`），页面通过 `window.AstrBotPluginView` bridge 调用，无需额外端口。

## 智能判断（默认开）

开启后，每条回复发出前会先问一次当前聊天模型（DeepSeek）：这条回复适合语音还是文字？

- 闲聊、情绪、关心、角色扮演 → **语音**，模型同时给出：
  - `script`：改写成适合朗读的口语稿（去掉 Markdown、表情、链接、列表）；
  - `instructions`：发音指导（情绪、语速、重音），作为 Breeze 的 `instructions` 参数；
- 代码、表格、链接、步骤教程、长文 → **文字**，保持原样。

模型返回 JSON，插件解析后调用 Breeze 合成。判断失败时降级为“直接转语音”。想退回“全部转语音”，把「智能判断」关掉即可。

## 配置

插件配置页（由 `_conf_schema.json` 自动生成）：

| 字段 | 说明 |
|---|---|
| `api_key` | Breeze API Key，`brz_` 开头（密文显示） |
| `voice_id` | 音色 ID，`voc_` 开头 |
| `auto_voice` | **全局语音回复，默认开** |
| `smart_voice` | 智能判断文字/语音，默认开 |
| `style_prompt` | 附加发音风格（可选） |
| `speed` | 语速 0.5–2.0 |
| `language_code` | 语言，默认 `zh` |
| `max_chars` | 单条上限，默认 200 字，超了保持文字 |
| `keep_text` | 语音之外是否同时保留文字 |
| `group_whitelist` | 只在这些群启用（逗号分隔，留空=全部） |
| `base_url` | 默认 `https://api.breeze.blue` |

> Breeze 返回的音频会被转成 **wav** 再发（AstrBot 的 `Record` 目前只接受 wav）。

## 指令

| 指令 | 说明 |
|---|---|
| `/breeze status` | 查看开关、音色、字数上限 |
| `/breeze on` / `/breeze off` | 开关全局语音回复（管理员，会写回配置） |
| `/breeze balance` | 查询账户积分余额 |
| `/breeze voices 女声` | 按关键词搜索音色 |
| `/breeze test 你好` | 用当前音色试听一句 |

## 本地跑起来

1. **起 AstrBot**（任选）：
   - Docker：`docker run -d -p 6185:6185 -p 6199:6199 -v $PWD/data:/AstrBot/data soulter/astrbot:latest`
   - 或用官方部署脚本 / 桌面客户端；
2. 打开 WebUI（默认 `http://localhost:6185`），在「消息平台」接入 NapCat（aiocqhttp）或其它平台；
3. 把本仓库放进 `AstrBot/data/plugins/`，WebUI 插件页点「重载」；
4. 插件配置里填 `api_key` 和 `voice_id`，保存；
5. 私聊或群里发一句话，机器人回复应该是语音。
   - 想单独试听：`/breeze test 你好`
   - 想确认余额：`/breeze balance`

### 需要注意

- **和 AstrBot 自带 TTS 不要同时开**：如果 AstrBot 的 TTS 已经接管，消息里会先有语音，本插件检测到后不再处理。建议用本插件时关掉自带 TTS。
- **Docker 部署**：语音以本地文件路径发送，AstrBot 与 NapCat 需要能访问同一个 `data` 目录（通常挂同一个 volume）；否则改用同机部署。
- 平台支持：QQ 个人号（aiocqhttp）、Telegram、企微、飞书、Discord、Slack 等支持语音；QQ 官方机器人、钉钉不支持语音，插件会自动跳过。
- 配置和数据都保存在 `data/` 下，重装插件不会丢。

## 发布

1. 推到 GitHub，仓库根目录就是插件目录（`main.py`、`metadata.yaml`、`_conf_schema.json`）；
2. 打开 [AstrBot 插件发布页](https://cloud.astrbot.app/publish)，用 AstrBot Cloud 账号提交仓库；
3. 市场会读取 `metadata.yaml` 展示名称、描述、版本、支持平台；
4. 之后每次发版改 `metadata.yaml` 的 `version` 并推 tag 即可。

发布前建议检查：

```bash
python3 -m json.tool _conf_schema.json > /dev/null   # schema 合法
python3 -m py_compile main.py                        # 语法通过
```

压缩包不要包含 `__pycache__`、`.git`、测试数据，体积控制在 16MB 内。
