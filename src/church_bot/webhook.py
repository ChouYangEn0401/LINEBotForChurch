"""LINE Webhook（選用）：讓「拿到群組 ID」變成在群組裡打一句話就好。

需要：
* .env 的 LINE_CHANNEL_SECRET
* LINE Developers 後台設定 Webhook URL（要公開的 https 網址；只有第一次抓 ID 時需要，
  可以用 cloudflared 暫時開一個，見 docs/SETUP_LINE.md）

支援的事件：
* 機器人被加進群組 → 在群組回覆群組 ID，並自動加到「群組表」（先不啟用，管理員確認後再打開）
* 在群組或私訊打「群組ID」→ 回覆這個聊天室的 ID
* 打「我的ID」→ 回覆自己的 userId（填到人員表就能被 @；填到設定就能收管理員通知）
* 機器人被踢出群組 → 通知管理員（那個群組以後收不到提醒了）

一般聊天內容一律不回應，不會吵到群組。回覆（Reply API）不計入 LINE 每月額度。
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import unicodedata

from church_bot.config import load_settings
from church_bot.errors import ChurchBotError, ConfigError
from church_bot.messengers.line import LineMessenger
from church_bot.models import OutgoingMessage, Target
from church_bot.service import BotService
from church_bot.tables import TABLE_WRITE_LOCK, TargetTable

log = logging.getLogger(__name__)

CMD_CHAT_ID = {"群組id", "群id", "id", "/id", "chatid", "群組代號"}
CMD_MY_ID = {"我的id", "myid", "/me", "userid"}
CMD_HELP = {"/help", "機器人說明", "提醒小幫手"}

HELP_TEXT = (
    "我是服事提醒小幫手 🙌\n"
    "・打「群組ID」：告訴你這個聊天室的 ID\n"
    "・打「我的ID」：告訴你自己的 ID\n"
    "其他訊息我都不會回，不會吵到大家 😊"
)


def verify_signature(channel_secret: str, body: bytes, signature: str) -> bool:
    digest = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode("ascii"), signature or "")


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").replace(" ", "").lower()


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
                    self._handle_event(event, messenger, settings.line.admin_target_id)
                except ChurchBotError as exc:
                    log.error("處理 LINE 事件失敗：%s", exc)
        finally:
            messenger.close()
        return len(events)

    # ------------------------------------------------------------------ events

    def _handle_event(self, event: dict, messenger: LineMessenger, admin_id: str) -> None:
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
        elif etype == "leave":
            history.remember_chat(chat_id, kind, status="left")
            log.warning("機器人被移出群組：%s", chat_id)
            self._alert_left(chat_id, messenger, admin_id)
        elif etype == "follow":
            history.remember_chat(chat_id, "user")
            messenger.reply(reply_token, f"謝謝你加我好友 🙌\n你的 LINE ID：\n{chat_id}\n\n{HELP_TEXT}")
        elif etype == "message" and event.get("message", {}).get("type") == "text":
            if kind in ("group", "room"):
                history.remember_chat(chat_id, kind)
            self._handle_command(_normalize(event["message"].get("text", "")), source, chat_id, kind,
                                 reply_token, messenger)

    def _handle_command(self, text: str, source: dict, chat_id: str, kind: str, reply_token: str,
                        messenger: LineMessenger) -> None:
        if text in CMD_CHAT_ID:
            label = {"group": "群組", "room": "聊天室", "user": "你的"}.get(kind, "")
            messenger.reply(reply_token, f"這個{label} ID：\n{chat_id}")
        elif text in CMD_MY_ID:
            uid = source.get("userId")
            messenger.reply(reply_token, f"你的 LINE ID：\n{uid}" if uid else
                            "抓不到你的 ID（電腦版 LINE 不會提供），請用手機 LINE 再打一次「我的ID」。")
        elif text in CMD_HELP:
            messenger.reply(reply_token, HELP_TEXT)

    # ------------------------------------------------------------------ helpers

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
