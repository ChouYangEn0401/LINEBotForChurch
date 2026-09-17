"""LINE Webhook（選用）：讓「拿到群組 ID」變成在群組裡打一句話就好。

需要：
* .env 的 LINE_CHANNEL_SECRET
* LINE Developers 後台設定 Webhook URL（要公開的 https 網址；只有第一次抓 ID 時需要，
  可以用 cloudflared 暫時開一個，見 docs/SETUP_LINE.md）

指令一律「/」開頭（全形「／」也可以），一般聊天不會誤觸：
* /群組ID → 回覆這個聊天室的 ID
* /我的ID → 回覆自己的名字＋userId（填到人員表就能被 @；填到設定就能收管理員通知）
* /說明 → 列出指令

支援的事件：
* 機器人被加進群組 → 在群組回覆群組 ID，並自動加到「群組表」（先不啟用，管理員確認後再打開）
* 有新成員加入群組 → 在群組回報新成員的名字＋userId（只有手機版 LINE 使用者才會有 userId）
* 機器人被踢出群組 → 通知管理員（那個群組以後收不到提醒了）
* 任何人在群組講話 → 背景記錄他的名字＋userId（不用開口特別問，講一句話就記住），
  在「同工名單」頁會看到「LINE 帳號」，一鍵就能加進人員表

一般聊天內容一律不回應，不會吵到群組。回覆（Reply API）、查名字（Get profile）都不計入 LINE 每月額度。
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import re
import unicodedata
from dataclasses import dataclass

from church_bot.config import Settings, load_settings
from church_bot.errors import ChurchBotError, ConfigError
from church_bot.messengers.line import LineMessenger
from church_bot.models import OutgoingMessage, Target
from church_bot.service import BotService
from church_bot.tables import TABLE_WRITE_LOCK, TargetTable

log = logging.getLogger(__name__)

# 指令名稱 → 可以打的寫法（比對時忽略大小寫、空白、全形半形）
COMMAND_WORDS: dict[str, tuple[str, ...]] = {
    "chat_id": ("群組id", "群id", "群組代號", "groupid", "chatid", "id"),
    "my_id": ("我的id", "myid", "me", "userid"),
    "help": ("說明", "指令", "help", "?"),
    "my_name": ("我的名字", "名字", "myname", "name"),
}
NO_ARGUMENT = {"chat_id", "my_id", "help"}
_WORD_TO_COMMAND = {word: name for name, words in COMMAND_WORDS.items() for word in words}
# 長的寫法先比，避免短的寫法把長的吃掉
_ARGUMENT_WORDS = sorted(((w, n) for w, n in _WORD_TO_COMMAND.items() if n not in NO_ARGUMENT),
                         key=lambda pair: -len(pair[0]))
NAME_MAX_LENGTH = 20

HELP_TEXT = (
    "我是服事提醒小幫手 🙌 指令都是「/」開頭：\n"
    "・/群組ID：這個聊天室的 ID\n"
    "・/我的ID：你自己的 ID\n"
    "・/我的名字 王小明：登記你的真實姓名（管理員開放時才能用）\n"
    "・/說明：顯示這段說明\n"
    "不是「/」開頭的訊息我都不會回，不會吵到大家 😊"
)


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    arg: str = ""


def parse_command(text: str) -> Command | None:
    """「/我的ID」「／群組 ID」「/我的名字 王小明」→ Command；不是「/」開頭或不認得的指令 → None。"""
    normalized = unicodedata.normalize("NFKC", text or "").strip()
    if not normalized.startswith("/"):
        return None
    compact = re.sub(r"\s+", "", normalized[1:]).lower()
    if _WORD_TO_COMMAND.get(compact) in NO_ARGUMENT:
        return Command(_WORD_TO_COMMAND[compact])
    body = normalized[1:].lstrip()
    for word, name in _ARGUMENT_WORDS:
        if body[: len(word)].lower() != word:
            continue
        rest = body[len(word):]
        if word.isascii() and rest and not (rest[0].isspace() or rest[0] in "=:"):
            continue  # 「/names」不是「/name」；中文指令後面直接接名字（/我的名字王小明）則可以
        return Command(name, rest.lstrip().lstrip("=:").strip())
    return None


@dataclass(slots=True)
class _Chat:
    """處理一則訊息需要的東西：設定、回覆方式、在哪裡、誰說的。"""

    settings: Settings
    messenger: LineMessenger
    reply_token: str
    chat_id: str
    kind: str
    user_id: str
    display_name: str = ""

    def reply(self, text: str) -> None:
        self.messenger.reply(self.reply_token, text)


def verify_signature(channel_secret: str, body: bytes, signature: str) -> bool:
    digest = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode("ascii"), signature or "")


PROFILE_REFRESH = dt.timedelta(days=1)


def _checked_recently(stamp: str) -> bool:
    try:
        checked = dt.datetime.fromisoformat(stamp)
    except ValueError:
        return False
    return dt.datetime.now().astimezone() - checked < PROFILE_REFRESH


class SignatureError(ChurchBotError):
    pass


class WebhookHandler:
    def __init__(self, service: BotService) -> None:
        self.service = service

    def handle(self, body: bytes, signature: str) -> int:
        """回傳處理了幾個事件。簽章不對丟 SignatureError；沒設定 secret 丟 ConfigError。"""
        settings = load_settings(self.service.paths)
        if not settings.line.channel_secret:
            raise ConfigError("收到 LINE Webhook，但沒有設定 LINE_CHANNEL_SECRET", "到「設定 → LINE 金鑰」填入 Channel secret。")
        if not verify_signature(settings.line.channel_secret, body, signature):
            raise SignatureError("LINE Webhook 簽章不符（Channel secret 可能填錯）", "確認「設定 → LINE 金鑰」的 Channel secret。")
        events = json.loads(body.decode("utf-8") or "{}").get("events", [])
        if not events:
            return 0  # LINE 後台按「Verify」時會送空的事件
        messenger = LineMessenger(settings.line.channel_access_token, settings.line.timeout_seconds)
        try:
            for event in events:
                try:
                    self._handle_event(event, messenger, settings)
                except ChurchBotError as exc:
                    log.error("處理 LINE 事件失敗：%s", exc)
        finally:
            messenger.close()
        return len(events)

    # ------------------------------------------------------------------ events

    def _handle_event(self, event: dict, messenger: LineMessenger, settings: Settings) -> None:
        etype = event.get("type")
        source = event.get("source", {})
        kind = source.get("type", "")
        chat_id = source.get("groupId") or source.get("roomId") or source.get("userId", "")
        reply_token = event.get("replyToken", "")
        history = self.service.history

        if etype == "join":
            name = messenger.group_name(chat_id) if kind == "group" else ""
            history.remember_chat(chat_id, kind, name)
            added = self._auto_add_target(chat_id, name)
            log.info("機器人被加進%s：%s（%s）", "群組" if kind == "group" else "聊天室", name or "?", chat_id)
            note = "已自動加到管理網頁的「群組」頁（尚未啟用）。" if added else "這個群組已經在「群組」頁裡了。"
            messenger.reply(reply_token, f"大家好！我是服事提醒小幫手 🙌\n這個群組的 ID：\n{chat_id}\n\n管理員：{note}")
        elif etype == "memberJoined":
            self._report_new_members(event, chat_id, kind, reply_token, messenger)
        elif etype == "leave":
            history.remember_chat(chat_id, kind, status="left")
            log.warning("機器人被移出群組：%s", chat_id)
            self._alert_left(chat_id, messenger, settings.line.admin_target_id)
        elif etype == "follow":
            history.remember_chat(chat_id, "user")
            name = self._touch_person(chat_id, chat_id, "user", messenger)
            greeting = f" 你好，{name}！" if name else ""
            messenger.reply(reply_token, f"謝謝你加我好友 🙌{greeting}\n你的 LINE ID：\n{chat_id}\n\n{HELP_TEXT}")
        elif etype == "message" and event.get("message", {}).get("type") == "text":
            if kind in ("group", "room"):
                history.remember_chat(chat_id, kind)
            user_id = source.get("userId", "")
            name = self._touch_person(user_id, chat_id, kind, messenger) if user_id else ""
            command = parse_command(event["message"].get("text", ""))
            if command is not None:
                chat = _Chat(settings, messenger, reply_token, chat_id, kind, user_id, name)
                self._handle_command(command, chat)

    def _handle_command(self, command: Command, chat: _Chat) -> None:
        if command.name == "chat_id":
            label = {"group": "群組", "room": "聊天室", "user": "你的"}.get(chat.kind, "")
            chat.reply(f"這個{label} ID：\n{chat.chat_id}")
        elif command.name == "my_id":
            if not chat.user_id:
                chat.reply("抓不到你的 ID（電腦版 LINE 不會提供），請用手機 LINE 再打一次「/我的ID」。")
            else:
                who = f"你的名字：{chat.display_name}\n" if chat.display_name else ""
                chat.reply(f"{who}你的 LINE ID：\n{chat.user_id}")
        elif command.name == "my_name":
            self._register_name(command.arg, chat)
        elif command.name == "help":
            chat.reply(HELP_TEXT)

    def _register_name(self, arg: str, chat: _Chat) -> None:
        """「/我的名字 王小明」：先記在資料庫，等管理員在「同工名單」頁按確認；後登記的蓋掉先登記的。"""
        if not chat.user_id:
            chat.reply("抓不到你的 LINE 帳號（電腦版 LINE 不會提供），請用手機 LINE 再打一次。")
            return
        real_name = re.sub(r"\s+", " ", arg).strip().strip("「」『』\"'").strip()
        history = self.service.history
        if not real_name:
            current = (history.person(chat.user_id) or {}).get("real_name", "")
            status = f"你登記過的名字：{current}（等管理員確認）\n" if current else ""
            chat.reply(f"{status}登記方式：打「/我的名字 王小明」（換成你的真實姓名）")
            return
        if not chat.settings.chat.collect_names:
            chat.reply("目前沒有開放登記名字 🙏 需要登記時，管理員會先打開這個功能。")
            return
        if len(real_name) > NAME_MAX_LENGTH:
            chat.reply(f"名字太長了（最多 {NAME_MAX_LENGTH} 個字），請再打一次。")
            return
        history.claim_real_name(chat.user_id, real_name)
        log.info("LINE 帳號登記名字：%s → %s（%s）", chat.display_name or "?", real_name, chat.user_id)
        line_name = f"（LINE 名稱：{chat.display_name}）" if chat.display_name else ""
        chat.reply(f"收到 🙌 已登記：{real_name}{line_name}\n"
                   "管理員確認後，服事提醒就會用這個名字對到你。打錯的話再打一次就會蓋掉。")

    def _report_new_members(self, event: dict, chat_id: str, kind: str, reply_token: str,
                            messenger: LineMessenger) -> None:
        members = event.get("joined", {}).get("members", [])
        uids = [m.get("userId") for m in members if m.get("type") == "user" and m.get("userId")]
        if not uids:
            return
        lines = []
        for uid in uids:
            name = self._touch_person(uid, chat_id, kind, messenger)
            lines.append(f"{name}（{uid}）" if name else uid)
        messenger.reply(reply_token, "歡迎新朋友加入 🙌\n" + "\n".join(lines))

    # ------------------------------------------------------------------ helpers

    def _touch_person(self, user_id: str, chat_id: str, kind: str, messenger: LineMessenger) -> str:
        """背景記錄一個人（被動收集，見檔案開頭說明），回傳目前已知的顯示名稱（可能是空字串）。

        顯示名稱最多一天向 LINE 查一次：有人改了 LINE 名稱，隔天講話就會更新；查不到也算查過，不會每句話都重查。
        """
        history = self.service.history
        known = history.person(user_id) or {}
        name = known.get("display_name", "")
        if not _checked_recently(known.get("profile_checked_at", "")):
            lookup_id = chat_id if kind in ("group", "room") else user_id
            fresh = messenger.member_profile(lookup_id, user_id)
            history.remember_person(user_id, fresh, chat_id, profile_checked=True)
            return fresh or name
        history.remember_person(user_id, "", chat_id)
        return name

    def _auto_add_target(self, chat_id: str, name: str) -> bool:
        table = TargetTable(self.service.paths.targets_file)
        with TABLE_WRITE_LOCK:
            items = table.load().items if table.path.exists() else []
            if any(t.line_id == chat_id for t in items):
                return False
            today = dt.date.today().isoformat()
            items.append(Target(name=name or f"新群組 {today}", line_id=chat_id, enabled=False,
                                note=f"機器人自動加入（{today}），確認後把「啟用」改成「是」"))
            table.save(items)
        return True

    def _alert_left(self, chat_id: str, messenger: LineMessenger, admin_id: str) -> None:
        targets = TargetTable(self.service.paths.targets_file)
        try:
            hit = next((t for t in targets.load().items if t.line_id == chat_id and t.enabled), None)
        except ChurchBotError:
            hit = None
        if hit is None or not admin_id:
            return
        text = (f"⚠️ 服事提醒機器人被移出「{hit.name}」群組了，這個群組以後收不到提醒。\n"
                "如果是不小心的，請重新邀請機器人進群組；不再需要的話，到管理網頁把這個群組停用。")
        try:
            messenger.send(admin_id, OutgoingMessage(text=text))
        except ChurchBotError as exc:
            log.error("通知管理員失敗：%s", exc)
