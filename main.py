import asyncio
import random
import re
from datetime import datetime, timedelta
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.api.star import Context, Star, register

STATE_KEY = "choujiang_state_v1"


@register("astrbot_plugin_choujiang", "GitHubCopilot", "群聊抽奖插件（报名、定时开奖、定时提醒）", "1.0.0")
class ChouJiangPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config: dict[str, Any] = config or {}
        self._state: dict[str, Any] = {"lotteries": {}}
        self._lock = asyncio.Lock()
        self._ticker_task: asyncio.Task | None = None
        self._check_interval_seconds = self._cfg_int("check_interval_seconds", 2, minimum=1, maximum=60)
        self._max_remind_mentions = self._cfg_int("max_remind_mentions", 50, minimum=1, maximum=200)
        self._default_remind_before_text = self._cfg_str("default_remind_before", "30m")
        self._default_remind_before_delta = self._parse_duration(self._default_remind_before_text)

    async def initialize(self):
        saved = await self.get_kv_data(STATE_KEY, {"lotteries": {}})
        if isinstance(saved, dict) and isinstance(saved.get("lotteries"), dict):
            self._state = saved
        self._ticker_task = asyncio.create_task(self._scheduler_loop())
        logger.info("[抽奖插件] 初始化完成")

    async def terminate(self):
        if self._ticker_task:
            self._ticker_task.cancel()
            try:
                await self._ticker_task
            except asyncio.CancelledError:
                pass
        await self._save_state()

    @filter.command("抽奖帮助")
    async def choujiang_help(self, event: AstrMessageEvent):
        """查看抽奖插件命令帮助"""
        yield event.plain_result(
            "抽奖命令:\n"
            "/抽奖创建 <开奖时间> | <奖品>\n"
            "  例: /抽奖创建 2026-05-03 20:00 | 京东卡100元\n"
            "/抽奖报名\n"
            "/抽奖名单\n"
            "/抽奖奖品 <新奖品>\n"
            "/抽奖开奖时间 <时间>\n"
            "/抽奖提醒时间 <时间>\n"
            "  也支持相对时间，如: 30m / 2h / 45s\n"
            "/抽奖提醒前 <时长>\n"
            "  例: /抽奖提醒前 30m (开奖前30分钟提醒未报名成员)\n"
            "/抽奖开奖 (立即开奖)\n"
            "/抽奖取消"
        )

    @filter.command("抽奖创建")
    async def choujiang_create(self, event: AstrMessageEvent):
        """创建一个新的群抽奖"""
        if not event.get_group_id():
            yield event.plain_result("该指令仅支持群聊使用。")
            return

        args = self._extract_args(event.message_str)
        if "|" not in args:
            yield event.plain_result("格式错误。用法: /抽奖创建 <开奖时间> | <奖品>")
            return

        draw_time_raw, prize_raw = args.split("|", 1)
        draw_time_raw = draw_time_raw.strip()
        prize = prize_raw.strip()
        draw_time = self._parse_time(draw_time_raw)
        now = datetime.now()

        if not draw_time:
            yield event.plain_result("开奖时间解析失败。支持: 2026-05-03 20:00 / 2026/05/03 20:00 / 30m")
            return
        if draw_time <= now:
            yield event.plain_result("开奖时间必须晚于当前时间。")
            return
        if not prize:
            yield event.plain_result("奖品不能为空。")
            return

        remind_time = self._compute_default_remind_time(draw_time, now)

        key = self._group_key(event)
        lottery = {
            "platform": event.get_platform_name(),
            "group_id": event.get_group_id(),
            "unified_msg_origin": event.unified_msg_origin,
            "creator_id": event.get_sender_id(),
            "creator_name": event.get_sender_name(),
            "self_id": event.get_self_id(),
            "prize": prize,
            "draw_time": draw_time.isoformat(),
            "remind_time": remind_time.isoformat() if remind_time else None,
            "reminded": False,
            "participants": {},
            "status": "open",
            "winner": None,
            "drawn_at": None,
        }

        async with self._lock:
            self._state["lotteries"][key] = lottery
            await self._save_state()

        remind_desc = (
            f"\n默认提醒时间: {remind_time.strftime('%Y-%m-%d %H:%M:%S')}"
            if remind_time
            else "\n默认提醒未启用（可在插件配置中设置 default_remind_before）"
        )
        yield event.plain_result(
            f"抽奖已创建。\n奖品: {prize}\n开奖时间: {draw_time.strftime('%Y-%m-%d %H:%M:%S')}{remind_desc}\n发送 /抽奖报名 参与抽奖。"
        )

    @filter.command("抽奖报名")
    async def choujiang_join(self, event: AstrMessageEvent):
        """报名当前群抽奖"""
        if not event.get_group_id():
            yield event.plain_result("该指令仅支持群聊使用。")
            return

        key = self._group_key(event)
        uid = event.get_sender_id()
        uname = event.get_sender_name() or uid

        async with self._lock:
            lottery = self._state["lotteries"].get(key)
            if not lottery or lottery.get("status") != "open":
                yield event.plain_result("当前群没有进行中的抽奖。")
                return
            participants = lottery.setdefault("participants", {})
            if uid in participants:
                yield event.plain_result("你已经报名过了。")
                return
            participants[uid] = uname
            await self._save_state()
            count = len(participants)

        yield event.plain_result(f"报名成功，当前已报名 {count} 人。")

    @filter.command("抽奖名单")
    async def choujiang_list(self, event: AstrMessageEvent):
        """查看当前抽奖报名名单"""
        if not event.get_group_id():
            yield event.plain_result("该指令仅支持群聊使用。")
            return

        key = self._group_key(event)
        lottery = self._state["lotteries"].get(key)
        if not lottery or lottery.get("status") != "open":
            yield event.plain_result("当前群没有进行中的抽奖。")
            return

        participants: dict[str, str] = lottery.get("participants", {})
        if not participants:
            yield event.plain_result("当前还没有人报名。")
            return

        lines = [f"已报名 {len(participants)} 人:"]
        for idx, (uid, name) in enumerate(participants.items(), start=1):
            lines.append(f"{idx}. {name} ({uid})")
        yield event.plain_result("\n".join(lines))

    @filter.command("抽奖奖品")
    async def choujiang_set_prize(self, event: AstrMessageEvent):
        """修改当前抽奖奖品"""
        if not event.get_group_id():
            yield event.plain_result("该指令仅支持群聊使用。")
            return

        prize = self._extract_args(event.message_str)
        if not prize:
            yield event.plain_result("用法: /抽奖奖品 <新奖品>")
            return

        key = self._group_key(event)
        async with self._lock:
            lottery = self._state["lotteries"].get(key)
            if not lottery or lottery.get("status") != "open":
                yield event.plain_result("当前群没有进行中的抽奖。")
                return
            lottery["prize"] = prize
            await self._save_state()

        yield event.plain_result(f"奖品已更新为: {prize}")

    @filter.command("抽奖开奖时间")
    async def choujiang_set_draw_time(self, event: AstrMessageEvent):
        """修改开奖时间"""
        if not event.get_group_id():
            yield event.plain_result("该指令仅支持群聊使用。")
            return

        raw = self._extract_args(event.message_str)
        draw_time = self._parse_time(raw)
        if not draw_time:
            yield event.plain_result("时间解析失败。示例: /抽奖开奖时间 2026-05-03 20:00")
            return
        if draw_time <= datetime.now():
            yield event.plain_result("开奖时间必须晚于当前时间。")
            return

        key = self._group_key(event)
        async with self._lock:
            lottery = self._state["lotteries"].get(key)
            if not lottery or lottery.get("status") != "open":
                yield event.plain_result("当前群没有进行中的抽奖。")
                return
            lottery["draw_time"] = draw_time.isoformat()
            remind_time = self._parse_iso_time(lottery.get("remind_time"))
            if remind_time and remind_time >= draw_time:
                remind_time = None
            if not remind_time:
                remind_time = self._compute_default_remind_time(draw_time, datetime.now())
            lottery["remind_time"] = remind_time.isoformat() if remind_time else None
            if remind_time:
                lottery["reminded"] = False
            await self._save_state()

        yield event.plain_result(f"开奖时间已更新为: {draw_time.strftime('%Y-%m-%d %H:%M:%S')}")

    @filter.command("抽奖提醒时间")
    async def choujiang_set_remind_time(self, event: AstrMessageEvent):
        """设置未报名提醒触发时间"""
        if not event.get_group_id():
            yield event.plain_result("该指令仅支持群聊使用。")
            return

        raw = self._extract_args(event.message_str)
        remind_time = self._parse_time(raw)
        if not remind_time:
            yield event.plain_result("时间解析失败。示例: /抽奖提醒时间 2026-05-03 19:30 或 /抽奖提醒时间 30m")
            return

        key = self._group_key(event)
        async with self._lock:
            lottery = self._state["lotteries"].get(key)
            if not lottery or lottery.get("status") != "open":
                yield event.plain_result("当前群没有进行中的抽奖。")
                return

            draw_time = self._parse_iso_time(lottery.get("draw_time"))
            now = datetime.now()
            if not draw_time:
                yield event.plain_result("当前抽奖数据异常，缺少开奖时间。")
                return
            if remind_time <= now:
                yield event.plain_result("提醒时间必须晚于当前时间。")
                return
            if remind_time >= draw_time:
                yield event.plain_result("提醒时间必须早于开奖时间。")
                return

            lottery["remind_time"] = remind_time.isoformat()
            lottery["reminded"] = False
            await self._save_state()

        yield event.plain_result(f"提醒时间已设置为: {remind_time.strftime('%Y-%m-%d %H:%M:%S')}")

    @filter.command("抽奖提醒前")
    async def choujiang_set_remind_before(self, event: AstrMessageEvent):
        """设置开奖前多久提醒未报名成员"""
        if not event.get_group_id():
            yield event.plain_result("该指令仅支持群聊使用。")
            return

        raw = self._extract_args(event.message_str)
        delta = self._parse_duration(raw)
        if not delta:
            yield event.plain_result("时长解析失败。示例: /抽奖提醒前 30m")
            return

        key = self._group_key(event)
        async with self._lock:
            lottery = self._state["lotteries"].get(key)
            if not lottery or lottery.get("status") != "open":
                yield event.plain_result("当前群没有进行中的抽奖。")
                return

            draw_time = self._parse_iso_time(lottery.get("draw_time"))
            now = datetime.now()
            if not draw_time:
                yield event.plain_result("当前抽奖数据异常，缺少开奖时间。")
                return

            remind_time = draw_time - delta
            if remind_time <= now:
                yield event.plain_result("提醒时间已过，请设置更短的提前时长或延后开奖时间。")
                return

            lottery["remind_time"] = remind_time.isoformat()
            lottery["reminded"] = False
            await self._save_state()

        yield event.plain_result(f"已设置开奖前提醒: {raw}（触发时间 {remind_time.strftime('%Y-%m-%d %H:%M:%S')}）")

    @filter.command("抽奖开奖")
    async def choujiang_draw_now(self, event: AstrMessageEvent):
        """手动立即开奖"""
        if not event.get_group_id():
            yield event.plain_result("该指令仅支持群聊使用。")
            return

        key = self._group_key(event)
        payload: dict[str, Any] | None = None
        async with self._lock:
            lottery = self._state["lotteries"].get(key)
            if not lottery or lottery.get("status") != "open":
                yield event.plain_result("当前群没有进行中的抽奖。")
                return
            payload = self._finalize_draw(lottery)
            await self._save_state()

        if payload:
            await self._send_draw_announcement(payload)

        yield event.plain_result("已手动开奖。")

    @filter.command("抽奖取消")
    async def choujiang_cancel(self, event: AstrMessageEvent):
        """取消当前群抽奖"""
        if not event.get_group_id():
            yield event.plain_result("该指令仅支持群聊使用。")
            return

        key = self._group_key(event)
        async with self._lock:
            existed = key in self._state["lotteries"]
            if existed:
                self._state["lotteries"].pop(key, None)
                await self._save_state()

        if existed:
            yield event.plain_result("当前群抽奖已取消。")
        else:
            yield event.plain_result("当前群没有进行中的抽奖。")

    async def _scheduler_loop(self):
        while True:
            try:
                await asyncio.sleep(self._check_interval_seconds)
                remind_jobs: list[dict[str, Any]] = []
                draw_jobs: list[dict[str, Any]] = []

                async with self._lock:
                    now = datetime.now()
                    changed = False

                    for lottery in self._state.get("lotteries", {}).values():
                        if lottery.get("status") != "open":
                            continue

                        draw_time = self._parse_iso_time(lottery.get("draw_time"))
                        if not draw_time:
                            continue

                        remind_time = self._parse_iso_time(lottery.get("remind_time"))
                        if remind_time and not lottery.get("reminded") and now >= remind_time:
                            lottery["reminded"] = True
                            remind_jobs.append(dict(lottery))
                            changed = True

                        if now >= draw_time:
                            draw_jobs.append(self._finalize_draw(lottery))
                            changed = True

                    if changed:
                        await self._save_state()

                for item in remind_jobs:
                    await self._send_unregistered_reminder(item)
                for item in draw_jobs:
                    await self._send_draw_announcement(item)
            except asyncio.CancelledError:
                break
            except Exception as ex:
                logger.exception(f"[抽奖插件] 定时任务异常: {ex}")

    async def _send_unregistered_reminder(self, lottery: dict[str, Any]):
        umo = str(lottery.get("unified_msg_origin") or "").strip()
        if not umo:
            return

        prize = str(lottery.get("prize") or "(未设置奖品)")
        draw_time = self._parse_iso_time(lottery.get("draw_time"))
        draw_text = draw_time.strftime("%Y-%m-%d %H:%M:%S") if draw_time else "未知"
        participants: dict[str, str] = lottery.get("participants", {}) or {}
        participant_ids = set(participants.keys())

        components: list[Any] = []

        members = await self._fetch_aiocqhttp_group_members(
            platform_name=str(lottery.get("platform") or ""),
            group_id=str(lottery.get("group_id") or ""),
        )

        if members:
            not_joined = []
            bot_self_id = str(lottery.get("self_id") or "")
            for member in members:
                uid = str(member.get("user_id") or "")
                if not uid or uid == bot_self_id:
                    continue
                if uid not in participant_ids:
                    not_joined.append(uid)

            if not_joined:
                shown = not_joined[: self._max_remind_mentions]
                for uid in shown:
                    components.append(Comp.At(qq=uid))
                remain = len(not_joined) - len(shown)
                tail = (
                    f" 抽奖提醒：奖品【{prize}】将在 {draw_text} 开奖，还没报名请发送 /抽奖报名。"
                )
                if remain > 0:
                    tail += f"（另外还有 {remain} 位未报名成员）"
                components.append(Comp.Plain(tail))
            else:
                components.append(Comp.Plain("抽奖提醒：当前群成员似乎都已报名，祝大家好运。"))
        else:
            components.append(Comp.AtAll())
            components.append(
                Comp.Plain(
                    f" 抽奖提醒：奖品【{prize}】将在 {draw_text} 开奖，还没报名请发送 /抽奖报名。"
                )
            )

        await self._send_chain(
            umo,
            components,
            platform_name=str(lottery.get("platform") or ""),
            group_id=str(lottery.get("group_id") or ""),
        )

    async def _send_draw_announcement(self, payload: dict[str, Any]):
        umo = str(payload.get("unified_msg_origin") or "").strip()
        if not umo:
            return

        prize = str(payload.get("prize") or "(未设置奖品)")
        count = int(payload.get("participant_count") or 0)
        winner = payload.get("winner")

        components: list[Any] = [Comp.AtAll()]
        if winner:
            winner_id = str(winner.get("user_id") or "")
            winner_name = str(winner.get("name") or winner_id)
            components.append(Comp.Plain(f" 🎉抽奖开奖啦！\n奖品：{prize}\n参与人数：{count}\n中奖者："))
            if winner_id:
                components.append(Comp.At(qq=winner_id))
                components.append(Comp.Plain(f" ({winner_name})"))
            else:
                components.append(Comp.Plain(winner_name))
        else:
            components.append(Comp.Plain(f" 抽奖开奖时间到！\n奖品：{prize}\n本次无人报名，已流局。"))

        await self._send_chain(
            umo,
            components,
            platform_name=str(payload.get("platform") or ""),
            group_id=str(payload.get("group_id") or ""),
        )

    async def _send_chain(
        self,
        unified_msg_origin: str,
        components: list[Any],
        platform_name: str = "",
        group_id: str = "",
    ):
        chain = MessageChain(chain=list(components))
        try:
            sent = await self.context.send_message(unified_msg_origin, chain)
            if sent:
                return
            logger.warning(f"[抽奖插件] send_message 返回 False: umo={unified_msg_origin}")
        except Exception as ex:
            logger.exception(f"[抽奖插件] 主动发送消息失败: {ex}")

        # 某些群场景机器人没有 @全体 权限，去掉 @全体 后重试一次，避免整条通知丢失。
        stripped_components = self._strip_at_all_components(components)
        if len(stripped_components) != len(components):
            try:
                fallback_chain = MessageChain(chain=stripped_components)
                sent = await self.context.send_message(unified_msg_origin, fallback_chain)
                if sent:
                    logger.warning("[抽奖插件] 已使用去除@全体的降级消息发送成功")
                    return
            except Exception as ex:
                logger.warning(f"[抽奖插件] 去除@全体后重试仍失败: {ex}")

        # 最后兜底：aiocqhttp 直接按群号发送，绕开 UMO 路由问题。
        if platform_name == "aiocqhttp" and group_id:
            direct_components = stripped_components if stripped_components else components
            if await self._send_aiocqhttp_group_direct(group_id, direct_components):
                logger.warning("[抽奖插件] 已通过 aiocqhttp 群号直发兜底成功")
                return

        logger.warning(f"[抽奖插件] 消息发送最终失败: umo={unified_msg_origin}")

    @staticmethod
    def _strip_at_all_components(components: list[Any]) -> list[Any]:
        ret: list[Any] = []
        for comp in components:
            if isinstance(comp, Comp.AtAll):
                continue
            if isinstance(comp, Comp.At) and str(getattr(comp, "qq", "")).lower() == "all":
                continue
            ret.append(comp)
        return ret

    async def _send_aiocqhttp_group_direct(self, group_id: str, components: list[Any]) -> bool:
        try:
            adapter = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            if not adapter:
                return False

            meta = adapter.meta() if hasattr(adapter, "meta") else None
            platform_id = "aiocqhttp"
            if meta and getattr(meta, "id", None):
                platform_id = str(meta.id)

            session = MessageSession(
                platform_name=platform_id,
                message_type=MessageType.GROUP_MESSAGE,
                session_id=str(group_id),
            )
            await adapter.send_by_session(session, MessageChain(chain=list(components)))
            return True
        except Exception as ex:
            logger.warning(f"[抽奖插件] aiocqhttp 群号直发失败: {ex}")
            return False

    def _finalize_draw(self, lottery: dict[str, Any]) -> dict[str, Any]:
        participants: dict[str, str] = lottery.get("participants", {}) or {}
        winner: dict[str, str] | None = None
        if participants:
            winner_id = random.choice(list(participants.keys()))
            winner = {"user_id": winner_id, "name": participants.get(winner_id, winner_id)}

        lottery["status"] = "drawn"
        lottery["drawn_at"] = datetime.now().isoformat()
        lottery["winner"] = winner

        return {
            "unified_msg_origin": lottery.get("unified_msg_origin"),
            "platform": lottery.get("platform"),
            "group_id": lottery.get("group_id"),
            "prize": lottery.get("prize"),
            "participant_count": len(participants),
            "winner": winner,
        }

    async def _fetch_aiocqhttp_group_members(
        self,
        platform_name: str,
        group_id: str,
    ) -> list[dict[str, Any]]:
        if platform_name != "aiocqhttp" or not group_id:
            return []

        try:
            adapter = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            if not adapter:
                return []

            client = adapter.get_client() if hasattr(adapter, "get_client") else getattr(adapter, "bot", None)
            if not client:
                return []

            gid: int | str = int(group_id) if group_id.isdigit() else group_id

            call_action = getattr(client, "call_action", None)
            if callable(call_action):
                result = await call_action("get_group_member_list", group_id=gid)
            else:
                api = getattr(client, "api", None)
                api_call_action = getattr(api, "call_action", None)
                if not callable(api_call_action):
                    return []
                result = await api_call_action("get_group_member_list", group_id=gid)

            if isinstance(result, dict) and isinstance(result.get("data"), list):
                result = result["data"]

            if not isinstance(result, list):
                return []

            ret: list[dict[str, Any]] = []
            for item in result:
                if not isinstance(item, dict):
                    continue
                uid = str(item.get("user_id") or "")
                if not uid:
                    continue
                ret.append(
                    {
                        "user_id": uid,
                        "nickname": str(item.get("card") or item.get("nickname") or uid),
                    }
                )
            return ret
        except Exception as ex:
            logger.warning(f"[抽奖插件] 获取群成员失败，回退到 @全体 提醒: {ex}")
            return []

    async def _save_state(self):
        await self.put_kv_data(STATE_KEY, self._state)

    @staticmethod
    def _extract_args(message_str: str) -> str:
        parts = message_str.strip().split(maxsplit=1)
        if len(parts) <= 1:
            return ""
        return parts[1].strip()

    @staticmethod
    def _group_key(event: AstrMessageEvent) -> str:
        return f"{event.get_platform_name()}:group:{event.get_group_id()}"

    @staticmethod
    def _parse_iso_time(raw: Any) -> datetime | None:
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            return datetime.fromisoformat(raw)
        except Exception:
            return None

    @staticmethod
    def _parse_duration(raw: str) -> timedelta | None:
        text = (raw or "").strip().lower()
        m = re.fullmatch(r"(\d+)\s*([smhd])", text)
        if not m:
            return None
        value = int(m.group(1))
        unit = m.group(2)
        if unit == "s":
            return timedelta(seconds=value)
        if unit == "m":
            return timedelta(minutes=value)
        if unit == "h":
            return timedelta(hours=value)
        if unit == "d":
            return timedelta(days=value)
        return None

    def _parse_time(self, raw: str) -> datetime | None:
        text = (raw or "").strip()
        if not text:
            return None

        delta = self._parse_duration(text)
        if delta:
            return datetime.now() + delta

        formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
        ]
        for fmt in formats:
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None

    def _cfg_int(self, key: str, default: int, *, minimum: int, maximum: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except Exception:
            return default
        return max(minimum, min(maximum, value))

    def _cfg_str(self, key: str, default: str) -> str:
        try:
            value = str(self.config.get(key, default)).strip()
        except Exception:
            return default
        return value

    def _compute_default_remind_time(self, draw_time: datetime, now: datetime) -> datetime | None:
        delta = self._default_remind_before_delta
        if not delta:
            return None
        remind_time = draw_time - delta
        if remind_time <= now:
            return None
        return remind_time
