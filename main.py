"""Breeze TTS for AstrBot — turn AI replies into voice messages.

全局开关 `auto_voice`（默认开）开启后，插件在消息结果装饰阶段把纯文本回复
替换成一条语音（Record）。配置面板由 AstrBot 根据 `_conf_schema.json` 自动生成。
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import aiohttp

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

DEFAULT_BASE = "https://api.breeze.blue"
# 这些平台适配器不支持语音消息
NO_RECORD_PLATFORMS = {"qq_official", "qq_official_webhook", "dingtalk"}


class BreezeTTSPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.session: aiohttp.ClientSession | None = None
        self.temp_dir = Path("data") / "temp" / "breeze_tts"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

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

    async def synthesize(self, text: str) -> Path:
        """调用 Breeze 合成 wav，返回本地文件路径（Record 只接受 wav）。"""
        api_key = str(self._cfg("api_key", "")).strip()
        voice_id = str(self._cfg("voice_id", "")).strip()
        if not api_key or not voice_id:
            raise RuntimeError("未配置 api_key 或 voice_id")

        speed = float(self._cfg("speed", 1.0) or 1.0)
        language = str(self._cfg("language_code", "zh") or "zh")
        body = {"text": text, "language_code": language}
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

            max_chars = int(self._cfg("max_chars", 200) or 200)
            if len(text) > max_chars:
                logger.info(
                    "Breeze TTS: 跳过（%s 字超过上限 %s）", len(text), max_chars
                )
                return

            started = time.time()
            path = await self.synthesize(text)
            logger.info(
                "Breeze TTS: %s 字 -> %s（%.1fs）",
                len(text),
                path.name,
                time.time() - started,
            )

            if bool(self._cfg("keep_text", False)):
                chain.append(Comp.Record(file=str(path)))
            else:
                kept = [item for item in chain if not isinstance(item, Comp.Plain)]
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
        voice = self._cfg("voice_id", "未配置")
        limit = self._cfg("max_chars", 200)
        yield event.plain_result(
            f"Breeze TTS\n语音回复：{enabled}\n音色：{voice}\n单条上限：{limit} 字"
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
