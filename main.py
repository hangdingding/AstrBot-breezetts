"""Breeze TTS for AstrBot — turn AI replies into voice messages.

全局开关 `auto_voice`（默认开）开启后，插件在消息结果装饰阶段把纯文本回复
替换成一条语音（Record）。配置面板由 AstrBot 根据 `_conf_schema.json` 自动生成。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import aiohttp

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request

PLUGIN_NAME = "astrbot_plugin_breeze_tts"
DEFAULT_BASE = "https://api.breeze.blue"
# 这些平台适配器不支持语音消息（QQ 官方机器人支持，会转成 silk 发送）
NO_RECORD_PLATFORMS = {"dingtalk"}


def _extract_json(raw: str) -> dict:
    """从模型输出里取出第一个 JSON 对象，容忍 ```json 代码块。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"模型没有返回 JSON: {raw[:120]}")
    return json.loads(text[start : end + 1])


class BreezeTTSPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.session: aiohttp.ClientSession | None = None
        self.temp_dir = Path("data") / "temp" / "breeze_tts"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        # 可视化页面使用的后端 API（页面在 views/panel）
        if context is not None and hasattr(context, "register_web_api"):
            context.register_web_api(
                f"/{PLUGIN_NAME}/settings", self.api_get_settings, ["GET"], "Breeze settings"
            )
            context.register_web_api(
                f"/{PLUGIN_NAME}/settings/save", self.api_save_settings, ["POST"], "Save settings"
            )
            context.register_web_api(
                f"/{PLUGIN_NAME}/balance", self.api_balance, ["GET"], "Breeze balance"
            )
            context.register_web_api(
                f"/{PLUGIN_NAME}/voices", self.api_voices, ["GET"], "Breeze voices"
            )
            context.register_web_api(
                f"/{PLUGIN_NAME}/design", self.api_design, ["POST"], "Breeze voice design"
            )
            context.register_web_api(
                f"/{PLUGIN_NAME}/design/save", self.api_save_design, ["POST"], "Save designed voice"
            )

    # ---------- 基础设施 ----------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=180)
            )
        return self.session

    async def terminate(self):
        """插件卸载/停用时释放连接。"""
        if self.session and not self.session.closed:
            await self.session.close()

    def _cfg(self, key: str, default=None):
        value = self.config.get(key, default)
        return default if value is None or value == "" else value

    def _base(self) -> str:
        return str(self._cfg("base_url", DEFAULT_BASE)).rstrip("/")

    def _cleanup(self) -> None:
        cutoff = time.time() - 600
        for file in self.temp_dir.glob("*.wav"):
            try:
                if file.stat().st_mtime < cutoff:
                    file.unlink()
            except OSError:
                pass

    # ---------- Breeze API ----------

    async def synthesize(self, text: str, instructions: str = "") -> Path:
        """调用 Breeze 合成 wav，返回本地文件路径（Record 只接受 wav）。

        instructions 是发音指导（情绪、语速、重音），对应 Breeze 的 instructions 字段。
        """
        api_key = str(self._cfg("api_key", "")).strip()
        voice_id = str(self._cfg("voice_id", "")).strip()
        if not api_key or not voice_id:
            raise RuntimeError("未配置 api_key 或 voice_id")

        speed = float(self._cfg("speed", 1.0) or 1.0)
        language = str(self._cfg("language_code", "zh") or "zh")
        body = {"text": text, "language_code": language}
        if instructions:
            body["instructions"] = instructions[:500]
        if speed != 1.0:
            body["voice_settings"] = {"speed": speed}

        url = f"{self._base()}/v1/text-to-speech/{voice_id}/stream?output_format=wav"
        session = await self._ensure_session()
        async with session.post(
            url, json=body, headers={"xi-api-key": api_key}
        ) as response:
            if response.status != 200:
                detail = (await response.text())[:200]
                raise RuntimeError(f"Breeze {response.status}: {detail}")
            audio = await response.read()
        if not audio:
            raise RuntimeError("Breeze 返回空音频")

        out = self.temp_dir / f"{int(time.time())}_{uuid.uuid4().hex[:8]}.wav"
        out.write_bytes(audio)
        self._cleanup()
        return out.resolve()

    async def get_balance(self) -> float:
        api_key = str(self._cfg("api_key", "")).strip()
        if not api_key:
            raise RuntimeError("未配置 api_key")
        session = await self._ensure_session()
        async with session.get(
            f"{self._base()}/v1/balance", headers={"xi-api-key": api_key}
        ) as response:
            if response.status != 200:
                detail = (await response.text())[:200]
                raise RuntimeError(f"Breeze {response.status}: {detail}")
            data = await response.json()
        return float(data.get("balance", 0))

    async def search_voices(self, keyword: str, limit: int = 5) -> list[dict]:
        api_key = str(self._cfg("api_key", "")).strip()
        if not api_key:
            raise RuntimeError("未配置 api_key")
        params = {"page_size": str(limit)}
        if keyword:
            params["search"] = keyword
        session = await self._ensure_session()
        async with session.get(
            f"{self._base()}/v1/voices",
            params=params,
            headers={"xi-api-key": api_key},
        ) as response:
            if response.status != 200:
                detail = (await response.text())[:200]
                raise RuntimeError(f"Breeze {response.status}: {detail}")
            data = await response.json()
        return data.get("voices", []) or []

    # ---------- 语音导演 ----------

    DIRECTOR_PROMPT = """你是这个聊天机器人的语音导演。根据对话内容，判断助手的这条回复应该用文字还是语音发给用户。

判断规则：
- 适合语音：闲聊、情绪表达、关心、安慰、角色扮演、短句、口语化回复。
- 适合文字：代码、表格、链接、步骤教程、长文、需要复制或保存的内容、报错信息。

如果判断为语音：把回复改写成适合朗读的口语稿 script：
- 去掉 Markdown 标记、表情符号、链接、列表符号和括号注释；
- 保留语气词，句子短一些，读起来自然；
- 不要添加回复里没有的事实。

同时给出发音指导 instructions：描述情绪、语速、重音和停顿，用中文，60 字以内，写得像导演给配音演员的提示。

只输出一个 JSON 对象，不要代码块、不要解释：
{"mode":"voice","instructions":"...","script":"..."}
如果判断为文字，输出：{"mode":"text"}"""

    async def _direct(self, event: AstrMessageEvent, reply_text: str) -> dict:
        """让当前聊天模型判断文字/语音，并产出 instructions + script。"""
        user_text = str(getattr(event, "message_str", "") or "")[:1000]
        style = str(self._cfg("style_prompt", "") or "").strip()
        prompt = self.DIRECTOR_PROMPT
        if style:
            prompt += f"\n\n额外的风格要求：{style}"
        prompt += f"\n\n用户说：{user_text}\n助手回复：{reply_text[:2000]}"

        provider_id = await self.context.get_current_chat_provider_id(
            umo=event.unified_msg_origin
        )
        response = await self.context.llm_generate(
            chat_provider_id=provider_id, prompt=prompt
        )
        raw = str(getattr(response, "completion_text", "") or "")
        return _extract_json(raw)

    # ---------- 页面 API ----------

    def _persist(self) -> None:
        saver = getattr(self.config, "save_config", None)
        if callable(saver):
            saver()

    def _mask(self) -> str:
        key = str(self._cfg("api_key", ""))
        return f"••••{key[-4:]}" if key else ""

    async def _settings_payload(self) -> dict:
        balance = None
        try:
            balance = await self.get_balance()
        except Exception:
            balance = None
        voice_name = ""
        try:
            voice_id = str(self._cfg("voice_id", "")).strip()
            if voice_id and str(self._cfg("api_key", "")).strip():
                info = await self._request_json(
                    "GET", f"/v1/voices/{voice_id}", None, timeout=10
                )
                voice_name = str(info.get("name", ""))
        except Exception:
            voice_name = ""

        return {
            "hasApiKey": bool(str(self._cfg("api_key", "")).strip()),
            "apiKeyHint": self._mask(),
            "voiceId": str(self._cfg("voice_id", "")),
            "voiceName": voice_name,
            "autoVoice": bool(self._cfg("auto_voice", True)),
            "smartVoice": bool(self._cfg("smart_voice", True)),
            "stylePrompt": str(self._cfg("style_prompt", "") or ""),
            "speed": float(self._cfg("speed", 1.0) or 1.0),
            "maxChars": int(self._cfg("max_chars", 200) or 200),
            "keepText": bool(self._cfg("keep_text", False)),
            "language": str(self._cfg("language_code", "zh") or "zh"),
            "groupWhitelist": str(self._cfg("group_whitelist", "") or ""),
            "balance": balance,
        }

    async def _request_json(self, method: str, path: str, body=None, timeout: int = 60) -> dict:
        api_key = str(self._cfg("api_key", "")).strip()
        if not api_key:
            raise RuntimeError("未配置 api_key")
        session = await self._ensure_session()
        async with session.request(
            method,
            f"{self._base()}{path}",
            json=body,
            headers={"xi-api-key": api_key},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            if response.status != 200:
                detail = (await response.text())[:300]
                raise RuntimeError(f"Breeze {response.status}: {detail}")
            return await response.json()

    async def api_get_settings(self):
        return json_response(await self._settings_payload())

    async def api_save_settings(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")

        api_key = payload.get("apiKey")
        if api_key is not None:
            if not isinstance(api_key, str) or len(api_key) > 200:
                return error_response("API Key 格式不对")
            if api_key.strip():
                self.config["api_key"] = api_key.strip()

        voice_id = payload.get("voiceId")
        if voice_id is not None:
            if not isinstance(voice_id, str) or not voice_id.strip():
                return error_response("音色 ID 不能为空")
            self.config["voice_id"] = voice_id.strip()

        auto_voice = payload.get("autoVoice")
        if auto_voice is not None:
            if not isinstance(auto_voice, bool):
                return error_response("autoVoice 必须是布尔值")
            self.config["auto_voice"] = auto_voice

        smart_voice = payload.get("smartVoice")
        if smart_voice is not None:
            if not isinstance(smart_voice, bool):
                return error_response("smartVoice 必须是布尔值")
            self.config["smart_voice"] = smart_voice

        style_prompt = payload.get("stylePrompt")
        if style_prompt is not None:
            self.config["style_prompt"] = str(style_prompt).strip()[:300]

        speed = payload.get("speed")
        if speed is not None:
            try:
                value = float(speed)
            except (TypeError, ValueError):
                return error_response("语速必须是数字")
            if not 0.5 <= value <= 2.0:
                return error_response("语速需要在 0.5–2.0 之间")
            self.config["speed"] = value

        max_chars = payload.get("maxChars")
        if max_chars is not None:
            try:
                limit = int(max_chars)
            except (TypeError, ValueError):
                return error_response("字数上限必须是整数")
            if not 10 <= limit <= 2000:
                return error_response("字数上限需要在 10–2000 之间")
            self.config["max_chars"] = limit

        keep_text = payload.get("keepText")
        if keep_text is not None:
            if not isinstance(keep_text, bool):
                return error_response("keepText 必须是布尔值")
            self.config["keep_text"] = keep_text

        language = payload.get("language")
        if language is not None:
            code = str(language).strip()
            if code and not (len(code) == 2 and code.isalpha()):
                return error_response("语言代码需要是两位字母")
            self.config["language_code"] = code or "zh"

        whitelist = payload.get("groupWhitelist")
        if whitelist is not None:
            self.config["group_whitelist"] = str(whitelist).strip()

        self._persist()
        return json_response(await self._settings_payload())

    async def api_balance(self):
        try:
            balance = await self.get_balance()
            return json_response({"balance": balance})
        except Exception as error:
            return error_response(str(error), status_code=502)

    async def api_voices(self):
        keyword = request.query.get("search", "") or ""
        try:
            limit = request.query.get("limit", 8, type=int)
        except Exception:
            limit = 8
        if not str(self._cfg("api_key", "")).strip():
            return json_response({"voices": [], "needsKey": True})
        try:
            voices = await self.search_voices(str(keyword), max(1, min(limit, 20)))
        except Exception as error:
            return error_response(str(error), status_code=502)
        return json_response(
            {
                "voices": [
                    {
                        "id": item.get("voice_id", ""),
                        "name": item.get("name", ""),
                        "description": item.get("description", ""),
                        "language": item.get("language_code", ""),
                        "previewUrl": item.get("preview_url", ""),
                    }
                    for item in voices
                ]
            }
        )

    async def api_design(self):
        if not str(self._cfg("api_key", "")).strip():
            return error_response("请先设置 API Key")
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")
        description = str(payload.get("description", "")).strip()
        if not 3 <= len(description) <= 500:
            return error_response("音色描述需要 3–500 字")
        text = str(payload.get("text", "")).strip()
        if len(text) > 500:
            return error_response("试听台词不能超过 500 字")
        language = str(payload.get("language", "zh") or "zh").strip()
        if not (len(language) == 2 and language.isalpha()):
            language = "zh"

        body = {
            "voice_description": description,
            "language_code": language,
            "preview_count": 3,
        }
        if text:
            body["text"] = text
        try:
            data = await self._request_json(
                "POST", "/v1/voice-previews/design", body, timeout=180
            )
        except Exception as error:
            return error_response(str(error), status_code=502)

        previews = []
        for item in data.get("previews", []) or []:
            previews.append(
                {
                    "id": item.get("generated_voice_id", ""),
                    "mime": item.get("media_type", "audio/mpeg"),
                    "duration": item.get("duration_secs"),
                    "audio": item.get("audio_base_64", ""),
                }
            )
        if not previews:
            return error_response("没有生成可试听的预览", status_code=502)
        return json_response({"previews": previews, "text": data.get("text", "")})

    async def api_save_design(self):
        if not str(self._cfg("api_key", "")).strip():
            return error_response("请先设置 API Key")
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")
        generated_id = str(payload.get("id", "")).strip()
        if not generated_id:
            return error_response("缺少预览 ID")
        name = str(payload.get("name", "")).strip()[:80] or "我的音色"
        description = str(payload.get("description", "")).strip()[:1000]
        language = str(payload.get("language", "zh") or "zh").strip()
        if not (len(language) == 2 and language.isalpha()):
            language = "zh"

        body = {"voice_name": name, "language_code": language}
        if description:
            body["voice_description"] = description
        try:
            data = await self._request_json(
                "POST",
                f"/v1/voice-previews/{generated_id}/save",
                body,
                timeout=60,
            )
        except Exception as error:
            return error_response(str(error), status_code=502)

        voice_id = data.get("voice_id", "")
        if not voice_id:
            return error_response("保存成功但没有返回音色 ID", status_code=502)
        self.config["voice_id"] = voice_id
        self._persist()
        return json_response({"voiceId": voice_id, "name": data.get("name", name)})

    # ---------- 语音回复 ----------

    def _allowed_here(self, event: AstrMessageEvent) -> bool:
        if not bool(self._cfg("auto_voice", True)):
            return False
        platform = ""
        try:
            platform = str(event.get_platform_name() or "")
        except Exception:
            platform = ""
        if platform in NO_RECORD_PLATFORMS:
            logger.info("Breeze TTS: 平台 %s 不支持语音，跳过", platform)
            return False
        whitelist = str(self._cfg("group_whitelist", "") or "").strip()
        if whitelist:
            allowed = {item.strip() for item in whitelist.split(",") if item.strip()}
            group_id = ""
            try:
                group_id = str(event.get_group_id() or "")
            except Exception:
                group_id = ""
            if group_id and group_id not in allowed:
                return False
        return True

    @filter.on_decorating_result()
    async def decorate_reply(self, event: AstrMessageEvent):
        """把最终的纯文本回复换成语音消息。"""
        try:
            if not self._allowed_here(event):
                return
            result = event.get_result()
            if result is None:
                return
            chain = getattr(result, "chain", None)
            if not isinstance(chain, list) or not chain:
                return
            if any(isinstance(item, Comp.Record) for item in chain):
                return  # 已经是语音（例如 AstrBot 自带 TTS 先处理过）

            text = "".join(
                item.text for item in chain if isinstance(item, Comp.Plain) and item.text
            ).strip()
            if not text:
                return

            script = text
            instructions = ""
            if bool(self._cfg("smart_voice", True)):
                try:
                    decision = await self._direct(event, text)
                    if str(decision.get("mode", "")).lower() != "voice":
                        logger.info("Breeze TTS: 判断为文字发送")
                        return
                    script = str(decision.get("script") or "").strip() or text
                    instructions = str(decision.get("instructions") or "").strip()
                except Exception as error:
                    logger.error("Breeze TTS: 语音导演失败，按语音处理: %s", error)

            max_chars = int(self._cfg("max_chars", 200) or 200)
            if len(script) > max_chars:
                logger.info(
                    "Breeze TTS: 跳过（%s 字超过上限 %s）", len(script), max_chars
                )
                return

            started = time.time()
            path = await self.synthesize(script, instructions)
            logger.info(
                "Breeze TTS: %s 字 -> %s（%.1fs）",
                len(script),
                path.name,
                time.time() - started,
            )

            if bool(self._cfg("keep_text", False)):
                chain.append(Comp.Record(file=str(path)))
            else:
                kept = [
                    item
                    for item in chain
                    if not isinstance(item, (Comp.Plain, Comp.At))
                ]
                kept.append(Comp.Record(file=str(path)))
                result.chain = kept
        except Exception as error:  # 不让单条失败影响整条消息
            logger.error("Breeze TTS 转换失败: %s", error)

    # ---------- 指令 ----------

    @filter.command_group("breeze")
    def breeze(self):
        """Breeze 语音回复管理"""
        pass

    @breeze.command("status")
    async def breeze_status(self, event: AstrMessageEvent):
        """查看当前状态"""
        enabled = "开" if bool(self._cfg("auto_voice", True)) else "关"
        smart = "开" if bool(self._cfg("smart_voice", True)) else "关"
        voice = self._cfg("voice_id", "未配置")
        limit = self._cfg("max_chars", 200)
        yield event.plain_result(
            f"Breeze TTS\n语音回复：{enabled}\n智能判断：{smart}\n音色：{voice}\n单条上限：{limit} 字"
        )

    @breeze.command("on")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def breeze_on(self, event: AstrMessageEvent):
        """开启全局语音回复"""
        self.config["auto_voice"] = True
        self.config.save_config()
        yield event.plain_result("已开启全局语音回复。")

    @breeze.command("off")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def breeze_off(self, event: AstrMessageEvent):
        """关闭全局语音回复"""
        self.config["auto_voice"] = False
        self.config.save_config()
        yield event.plain_result("已关闭全局语音回复。")

    @breeze.command("balance")
    async def breeze_balance(self, event: AstrMessageEvent):
        """查询账户积分余额"""
        try:
            balance = await self.get_balance()
            yield event.plain_result(f"Breeze 余额：{balance:,.0f} 积分")
        except Exception as error:
            yield event.plain_result(f"查询失败：{error}")

    @breeze.command("voices")
    async def breeze_voices(self, event: AstrMessageEvent, keyword: str = ""):
        """按关键词搜索音色"""
        try:
            voices = await self.search_voices(keyword)
            if not voices:
                yield event.plain_result("没有找到音色。")
                return
            lines = [
                f"{item.get('name', '')}（{item.get('language_code', '')}）\n{item.get('voice_id', '')}"
                for item in voices[:5]
            ]
            yield event.plain_result("Breeze 音色：\n\n" + "\n\n".join(lines))
        except Exception as error:
            yield event.plain_result(f"查询失败：{error}")

    @breeze.command("test")
    async def breeze_test(self, event: AstrMessageEvent, text: str = ""):
        """用当前音色合成一句话试听"""
        sample = text.strip() or "你好，我是 Breeze 语音，很高兴认识你。"
        try:
            path = await self.synthesize(sample[:200])
            yield event.chain_result([Comp.Record(file=str(path))])
        except Exception as error:
            yield event.plain_result(f"合成失败：{error}")
