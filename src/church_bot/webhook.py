"""LINE Webhook（選用）：讓「拿到群組 ID」變成在群組裡打一句話就好。

需要：
* .env 的 LINE_CHANNEL_SECRET
* LINE Developers 後台設定 Webhook URL（要公開的 https 網址；只有第一次抓 ID 時需要，
  可以用 cloudflared 暫時開一個，見 docs/SETUP_LINE.md）

指令一律「/」開頭（全形「／」也可以），一般聊天不會誤觸：
* /群組ID → 回覆這個聊天室的 ID
* /我的ID → 回覆自己的名字＋userId（填到同工名單就能被 @；填到設定就能收管理員通知）
* /我的名字 王小明 → 登記真實姓名，等管理員確認（「收集名單」開著才能用）
* /我的暱稱 阿明 → 多登記一個稱呼（一個人可以有好幾個，不會蓋掉真實姓名）
* /設定 名稱=值 → 修改少數設定，要輸入一次性驗證碼（/驗證 123456、/取消；見 remote_config.py）
* /服務網址 → 管理網頁現在的網址（每次重開免費模式都會變，所以不用再一個一個貼給管理員）
* /提醒（或 /現在提醒）→ 在設定好的提醒群組裡，誰都可以打；立刻用 Reply 免費送出這週的提醒（不計入 LINE 額度）。
  排程時間前 2 天內打過、內容也一樣，排程就略過（見 History.skip_reason），服事表改過才會再送
* /別周測試 1004 → **只有管理員**：試印別一週的提醒，不記錄、不影響排程（見 service.preview_for）
* /權限 → 誰是管理員、每個指令誰能用
* /我的權限 → 打的人自己的狀況（對應到哪位同工、能不能被 @、是不是管理員）
* /說明（也可以打 /help、/?）→ 列出指令

「誰能用什麼」只有兩層，刻意不做複雜的權限系統：
* 大家都能用的指令，最多只會讓機器人「回一句話」或「記一個等管理員確認的名字」，不會改到設定或發送給別人。
* 會影響大家的（改設定、試印別週），靠「驗證碼只送給管理員」和「管理員 LINE ID」擋住。

支援的事件：
* 機器人被加進群組 → 在群組回覆群組 ID，並自動加到「LINE 群組」（先不啟用，管理員確認後再打開）
* 有新成員加入群組 → 在群組回報新成員的名字＋userId（只有手機版 LINE 使用者才會有 userId）
* 機器人被踢出群組 → 通知管理員（那個群組以後收不到提醒了）
* 任何人在群組講話 → 背景記錄他的名字＋userId（不用開口特別問，講一句話就記住），
  在「同工名單」頁會看到「LINE 帳號」，一鍵就能加進同工名單

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
import sys
import unicodedata
from dataclasses import dataclass
from typing import Callable, Iterable

from church_bot.config import Settings, load_settings
from church_bot.core.dates import parse_user_date
from church_bot.core.history import MAX_NICKNAMES, nicknames_of
from church_bot.errors import ChurchBotError, ConfigError
from church_bot.messengers.line import LineMessenger
from church_bot.models import Member, OutgoingMessage, Target
from church_bot.remote_config import (
    CODE_RE, CODE_TTL, PendingChange, Verifier, VerifyError, apply_change, describe_options, find_option,
    parse_assignment,
)
from church_bot.service import BotService
from church_bot.tables import TABLE_WRITE_LOCK, MemberTable, TargetTable

log = logging.getLogger(__name__)

# 指令名稱 → 可以打的寫法（比對時忽略大小寫、空白、全形半形）
COMMAND_WORDS: dict[str, tuple[str, ...]] = {
    "chat_id": ("群組id", "群id", "群組代號", "groupid", "chatid", "id"),
    "my_id": ("我的id", "myid", "me", "userid"),
    "help": ("說明", "指令", "幫助", "怎麼用", "help", "?"),
    "cancel": ("取消", "cancel"),
    "my_name": ("我的名字", "名字", "myname", "name"),
    "my_nickname": ("我的暱稱", "暱稱", "我的綽號", "綽號", "mynickname", "nickname", "nick"),
    "config": ("設定", "config", "setting", "set"),
    "verify": ("驗證碼", "驗證", "verify", "code"),
    "notify_now": ("提醒", "現在提醒", "立即提醒", "發提醒", "remind", "notifynow"),
    "permissions": ("權限顯示", "權限", "權限說明", "誰可以用", "permissions", "perms"),
    "my_permissions": ("我的權限", "我的資料", "myperms", "whoami"),
    "test_week": ("別周測試", "別週測試", "測試提醒", "測試", "testweek", "test"),
    "service_url": ("服務網址", "管理網址", "管理網頁", "網址", "網站", "後台", "serviceurl", "url", "site", "web"),
}
NO_ARGUMENT = {"chat_id", "my_id", "help", "cancel", "notify_now", "permissions", "my_permissions", "service_url"}
_WORD_TO_COMMAND = {word: name for name, words in COMMAND_WORDS.items() for word in words}
# 長的寫法先比，避免短的寫法把長的吃掉
_ARGUMENT_WORDS = sorted(((w, n) for w, n in _WORD_TO_COMMAND.items() if n not in NO_ARGUMENT),
                         key=lambda pair: -len(pair[0]))
NAME_MAX_LENGTH = 20

HELP_TEXT = (
    "我是服事提醒小幫手 🙌\n"
    "訊息開頭有「/」我才會回；平常聊天我一律不出聲，不會吵到大家 😊\n"
    "\n"
    "🟢 大家都可以打\n"
    "・/提醒\n"
    "　→ 馬上看這週誰服事（免費，不扣 LINE 額度）\n"
    "・/我的名字 王小明\n"
    "　→ 登記真實姓名，提醒才 @ 得到你\n"
    "・/我的暱稱 阿明\n"
    "　→ 再加一個稱呼；服事表寫暱稱也認得出是你\n"
    "・/我的ID　→ 你自己的 LINE ID\n"
    "・/群組ID　→ 這個聊天室的 ID\n"
    "・/服務網址　→ 管理網頁現在的網址（要密碼才進得去）\n"
    "・/我的權限　→ 你目前的狀況（有沒有對應到同工…）\n"
    "・/權限　→ 誰是管理員、哪個指令誰能用\n"
    "・/說明　→ 這段說明（/help、/? 也可以）\n"
    "\n"
    "🔐 要驗證碼（驗證碼只給管理員）\n"
    "・/設定　→ 改機器人設定（收集名單、自動發送、每幾週）\n"
    "\n"
    "👑 只有管理員\n"
    "・/別周測試 1004　→ 試印 10/4 那一週，不會真的發出去\n"
    "\n"
    "打錯或不認得的指令我不會回，直接再打一次就好 🙏"
)


EVERYONE_COMMANDS = "/提醒、/我的名字、/我的暱稱、/我的ID、/群組ID、/服務網址、/說明、/權限、/我的權限"
ADMIN_COMMANDS = "/別周測試"
TEST_WEEK_USAGE = ("用法：/別周測試 1004\n（10/04、10-4、10月4日、2026/10/4 都可以）\n"
                   "會在你打指令的這個聊天室試印那一天起這一週的提醒：不會發到其他群組、不會 @ 別的群組的人、"
                   "也不影響每週的自動提醒。想測得安靜一點就私訊機器人打。")


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    arg: str = ""


def is_admin(settings: Settings, user_id: str, chat_id: str, admin_ids: Iterable[str] = ()) -> bool:
    """管理員 = 同工名單裡勾了「管理員」的人（admin_ids 是他們的 LINE ID），
    或設定裡「出問題通知誰」的那個人，或在那個管理員群組裡講話。

    刻意不做多層權限：真正會影響大家的事（改設定）另外靠一次性驗證碼，驗證碼也只送給管理員。
    """
    admin = (settings.line.admin_target_id or "").strip()
    if admin and admin in {user_id, chat_id}:
        return True
    return bool(user_id) and user_id in set(admin_ids)


def permissions_text(settings: Settings, admin_name: str = "", admin_names: Iterable[str] = ()) -> str:
    """「/權限」的回覆：誰是管理員、哪個指令誰能用、機器人不會做什麼。

    ``admin_names`` 是同工名單裡勾了管理員的人；``admin_name`` 是設定裡「出問題通知誰」那個人的名字（查得到的話）。
    """
    admin = (settings.line.admin_target_id or "").strip()
    names = list(dict.fromkeys([*admin_names, *([admin_name] if admin_name else [])]))
    if not names and admin:
        names = [f"{admin[:5]}…{admin[-4:]}"]  # 只露頭尾，不把整個 ID 貼在群組裡
    if names:
        admin_line = f"・管理員：{'、'.join(names)}"
        admin_note = "　拿得到驗證碼、收得到錯誤通知、可以打 /別周測試、可以開管理網頁"
    else:
        admin_line = "・管理員：還沒設定 ⚠️"
        admin_note = "　到管理網頁「同工名單」把自己勾成管理員（要先對應好 LINE 帳號）"
    switches = (f"收集名單「{'開' if settings.chat.collect_names else '關'}」"
                f"・自動發送「{'開' if settings.schedule.enabled else '關'}」"
                f"・用 LINE 改設定「{'開' if settings.chat.remote_config else '關'}」")
    return "\n".join([
        "🔐 誰可以做什麼",
        "",
        admin_line,
        admin_note,
        "",
        "・大家（這個群組裡任何人）：",
        f"　{EVERYONE_COMMANDS}",
        "　這些最多只是「回一句話」或「記一個等管理員確認的名字」，改不到設定、也不會發訊息給別人。",
        "　/提醒 是免費的回覆，所以不限制誰能打。",
        "",
        "・要驗證碼才算數：/設定",
        "　驗證碼只會送給管理員，等於要管理員同意才改得動。",
        "",
        f"・只有管理員：{ADMIN_COMMANDS}",
        "",
        "・我不會做的事：不回一般聊天、不刪訊息、不拿聊天內容做別的事。",
        "　我只記「誰講過話」的名字和 ID，用來對應服事表上的名字。",
        "",
        f"・目前狀態：{switches}",
        "　要改：打「/設定」，或請管理員開管理網頁。",
    ])


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


def _print_to_screen(text: str) -> None:
    """印在執行程式的視窗上（不寫進記錄檔）。沒有視窗（背景執行）時什麼都不做。"""
    if sys.stdout is None:
        return
    try:
        print(text, flush=True)
    except (UnicodeEncodeError, OSError):
        print(text.encode("ascii", errors="replace").decode("ascii"), flush=True)


class WebhookHandler:
    def __init__(self, service: BotService, verifier: Verifier | None = None,
                 on_settings_changed: Callable[[], None] | None = None) -> None:
        self.service = service
        self.verifier = verifier or Verifier()
        self.on_settings_changed = on_settings_changed

    def handle(self, body: bytes, signature: str, public_url: str = "") -> int:
        """回傳處理了幾個事件。簽章不對丟 SignatureError；沒設定 secret 丟 ConfigError。

        ``public_url`` = LINE 剛剛打到的那個對外網址（網頁那一層從請求的 Host 推出來）。
        一定要等簽章驗過才記：不然誰都能偽造一個 Host，把假網址餵給「/服務網址」。
        """
        settings = load_settings(self.service.paths)
        if not settings.line.channel_secret:
            raise ConfigError("收到 LINE Webhook，但沒有設定 LINE_CHANNEL_SECRET", "到「設定 → 金鑰與密碼」填入 Channel secret。")
        if not verify_signature(settings.line.channel_secret, body, signature):
            raise SignatureError("LINE Webhook 簽章不符（Channel secret 可能填錯）", "確認「設定 → 金鑰與密碼」的 Channel secret。")
        if public_url:
            self.service.remember_service_url(public_url)
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
            note = "已自動加到管理網頁的「LINE 群組」頁（尚未啟用）。" if added else "這個群組已經在「LINE 群組」頁裡了。"
            messenger.reply(reply_token, f"大家好！我是服事提醒小幫手 🙌\n這個群組的 ID：\n{chat_id}\n\n管理員：{note}")
        elif etype == "memberJoined":
            self._report_new_members(event, chat_id, kind, reply_token, messenger)
        elif etype == "leave":
            history.remember_chat(chat_id, kind, status="left")
            log.warning("機器人被移出群組：%s", chat_id)
            self._alert_left(chat_id, messenger, self.service.admin_targets(settings, self._members()))
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
            text = event["message"].get("text", "")
            command = parse_command(text)
            if command is None and user_id and self.verifier.waiting_for(user_id, chat_id):
                bare = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
                if CODE_RE.fullmatch(bare):  # 等驗證碼的人直接打 6 位數字也算
                    command = Command("verify", bare)
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
        elif command.name == "my_nickname":
            self._register_nickname(command.arg, chat)
        elif command.name == "permissions":
            chat.reply(permissions_text(chat.settings, self._admin_name(chat.settings), self._admin_names()))
        elif command.name == "my_permissions":
            self._my_permissions(chat)
        elif command.name == "test_week":
            self._test_week(command.arg, chat)
        elif command.name == "config":
            self._request_change(command.arg, chat)
        elif command.name == "verify":
            self._verify(command.arg, chat)
        elif command.name == "cancel":
            cancelled = bool(chat.user_id) and self.verifier.cancel(chat.user_id, chat.chat_id)
            chat.reply("已取消這次的設定修改。" if cancelled else "目前沒有等你驗證的設定修改。")
        elif command.name == "notify_now":
            self._notify_now(chat)
        elif command.name == "service_url":
            self._service_url(chat)
        elif command.name == "help":
            chat.reply(HELP_TEXT)

    # ------------------------------------------------------------------ /設定（見 remote_config.py）

    def _request_change(self, arg: str, chat: _Chat) -> None:
        settings = chat.settings
        if not settings.chat.remote_config:
            chat.reply("目前沒有開放用 LINE 修改設定（管理網頁「設定 → LINE 聊天室指令」可以打開）。")
            return
        if not arg:
            chat.reply(describe_options(settings))
            return
        if not chat.user_id:
            chat.reply("抓不到你的 LINE 帳號（電腦版 LINE 不會提供），請用手機 LINE 再打一次。")
            return
        key, value_text = parse_assignment(arg)
        option = find_option(key)
        if option is None:
            chat.reply(f"沒有「{key}」這個設定。\n\n{describe_options(settings)}")
            return
        if not value_text:
            chat.reply(f"「{option.key}」現在是「{option.current(settings)}」。\n{option.hint}\n\n"
                       f"要改請打：/設定 {option.key}=新的值")
            return
        try:
            value = option.parse(value_text)
        except ValueError as exc:
            chat.reply(f"「{option.key}」的值看不懂：{exc}")
            return
        if option.show(value) == option.current(settings):
            chat.reply(f"「{option.key}」本來就是「{option.show(value)}」，不用改。")
            return
        chat_info = self.service.history.chat(chat.chat_id) or {}
        label = chat_info.get("name") or {"group": "群組", "room": "多人聊天室", "user": "私訊"}.get(chat.kind, "")
        try:
            pending, code = self.verifier.start(option, value, user_id=chat.user_id, chat_id=chat.chat_id,
                                                requester=chat.display_name, chat_label=label)
        except VerifyError as exc:
            chat.reply(exc.message)
            return
        where = self._deliver_code(pending, code, chat)
        minutes = int(CODE_TTL.total_seconds() // 60)
        chat.reply(f"🔐 要把「{option.key}」改成「{pending.value_text}」，需要驗證碼。\n{where}\n"
                   f"請在 {minutes} 分鐘內打「/驗證 六位數字」（或直接打那 6 個數字）。打 /取消 可以取消。")

    def _deliver_code(self, pending: PendingChange, code: str, chat: _Chat) -> str:
        """把驗證碼送到打指令的人以外的地方，回傳「驗證碼在哪裡」的說明。"""
        who = pending.requester or pending.user_id
        _print_to_screen(f"\n🔐 [LINE 設定驗證碼] {who}（{pending.chat_label}）要把「{pending.option.key}」"
                         f"改成「{pending.value_text}」→ 驗證碼：{code}（5 分鐘內有效，只能用一次）\n")
        log.warning("LINE 設定修改等待驗證：%s（%s）要把「%s」改成「%s」", who, pending.chat_label, pending.option.key,
                    pending.value_text)
        on_screen = "驗證碼顯示在執行機器人的電腦畫面上（管理網頁也可以直接核准）。"
        targets = self.service.admin_targets(chat.settings, self._members())
        if not (chat.settings.chat.send_code_to_admin and targets):
            return on_screen
        if not self.verifier.allow_push():
            log.warning("今天用 LINE 私訊驗證碼的次數已達上限，這次只顯示在電腦畫面上")
            return on_screen
        text = (f"🔐 有人要用 LINE 修改機器人設定\n・誰：{who}\n・在哪裡：{pending.chat_label}\n"
                f"・要改：{pending.option.key} → {pending.value_text}\n\n驗證碼：{code}\n\n"
                "5 分鐘內有效、只能用一次。是你同意的修改才把驗證碼告訴對方；不是的話不用理它，時間到自動失效。")
        try:
            for target in targets:
                chat.messenger.send(target, OutgoingMessage(text=text))
        except ChurchBotError as exc:
            log.error("用 LINE 私訊驗證碼給管理員失敗：%s", exc)
            return on_screen
        return "驗證碼已經私訊給管理員，也顯示在執行機器人的電腦畫面上。"

    def _verify(self, arg: str, chat: _Chat) -> None:
        code = re.sub(r"\s+", "", arg)
        if not CODE_RE.fullmatch(code):
            chat.reply("驗證碼是 6 位數字，例如：/驗證 123456")
            return
        try:
            pending = self.verifier.submit(chat.user_id, chat.chat_id, code)
        except VerifyError as exc:
            chat.reply(exc.message)
            return
        self.apply(pending)
        chat.reply(f"✅ 已更新：{pending.option.key} → {pending.value_text}")

    def apply(self, pending: PendingChange) -> None:
        apply_change(self.service.paths, pending)
        if pending.option.reschedule and self.on_settings_changed is not None:
            self.on_settings_changed()

    # ------------------------------------------------------------------ /提醒（免費 Reply，見 service.notify_now）

    def _notify_now(self, chat: _Chat) -> None:
        # 誰都可以打：Reply 免費，而且只會回在「LINE 群組」頁啟用的群組（service.notify_now 會檢查）
        messages, note = self.service.notify_now(
            chat.chat_id, send=lambda items: chat.messenger.reply_texts(chat.reply_token, items))
        if not messages:
            chat.reply(note)

    # ------------------------------------------------------------------ /服務網址

    def _service_url(self, chat: _Chat) -> None:
        """「/服務網址」：回管理網頁現在的網址（見 core/public_url.py）。

        誰都可以問，因為進得去還要密碼：換網址的人只要更新一次，其他管理員自己來問就好，不用一個一個貼。
        還沒設密碼時只回給管理員——那種狀態下，拿到網址的人就能改設定。
        """
        current = self.service.service_url()
        if not current.known:
            chat.reply("我還不知道對外的網址 🤔\n"
                       "請管理員在那台電腦上開「免費模式」（3-open-webhook），開好之後再打一次「/服務網址」。")
            return
        if not chat.settings.web.password and not self._is_admin(chat):
            chat.reply("管理網頁現在還沒設密碼，誰點進去都能改設定，所以我先不公開網址 🙏\n"
                       "請管理員到「設定 → 金鑰與密碼」設一個密碼，之後大家打「/服務網址」就拿得到。")
            return
        warning = "" if chat.settings.web.password else "\n⚠️ 這個網站目前還沒設密碼，請管理員盡快設一個。"
        chat.reply(f"🖥 管理網頁現在的網址：\n{current.url}\n\n"
                   "・要密碼才進得去，密碼問管理員\n"
                   "・網址每次重開都會變，隨時打「/服務網址」就會給你最新的\n"
                   "・那台電腦關機、或關掉免費模式的時候連不進去\n"
                   f"（{current.describe()}）{warning}")

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

    def _register_nickname(self, arg: str, chat: _Chat) -> None:
        """「/我的暱稱 阿明」：一個人可以有好幾個稱呼，都會對到同一位同工。

        不動他登記的真實姓名（那是 /我的名字），也一樣要管理員在「同工名單」頁按一下才會生效。
        """
        if not chat.user_id:
            chat.reply("抓不到你的 LINE 帳號（電腦版 LINE 不會提供），請用手機 LINE 再打一次。")
            return
        nickname = re.sub(r"\s+", " ", arg).strip().strip("「」『』\"'").strip()
        history = self.service.history
        if not nickname:
            current = nicknames_of(history.person(chat.user_id))
            status = f"你登記過的暱稱：{'、'.join(current)}（等管理員確認）\n" if current else ""
            chat.reply(f"{status}登記方式：打「/我的暱稱 阿明」（換成大家平常怎麼叫你）\n"
                       "服事表寫這個稱呼時，機器人就知道是你。真實姓名請用「/我的名字」。")
            return
        if not chat.settings.chat.collect_names:
            chat.reply("目前沒有開放登記名字 🙏 需要登記時，管理員會先打開這個功能。")
            return
        if len(nickname) > NAME_MAX_LENGTH:
            chat.reply(f"暱稱太長了（最多 {NAME_MAX_LENGTH} 個字），請再打一次。")
            return
        problem = history.claim_nickname(chat.user_id, nickname)
        if problem == "duplicate":
            chat.reply(f"「{nickname}」你已經登記過了 👌 等管理員確認就會生效。")
            return
        if problem == "full":
            chat.reply(f"一個人最多登記 {MAX_NICKNAMES} 個暱稱 🙏 想換的話，請管理員到「同工名單」頁調整。")
            return
        log.info("LINE 帳號登記暱稱：%s → %s（%s）", chat.display_name or "?", nickname, chat.user_id)
        chat.reply(f"收到 🙌 已登記暱稱：{nickname}\n"
                   "管理員確認後，服事表寫這個稱呼也會對到你。還有別的叫法就再打一次「/我的暱稱 ○○」。")

    # ------------------------------------------------------------------ /權限、/我的權限

    def _members(self) -> list[Member]:
        try:
            return MemberTable(self.service.paths.members_file).load().items
        except ChurchBotError as exc:
            log.error("讀同工名單失敗：%s", exc)
            return []

    def _admin_ids(self) -> set[str]:
        """同工名單裡勾了「管理員」而且對應好 LINE 帳號的人。"""
        return {m.line_user_id for m in self._members() if m.admin and m.line_user_id}

    def _admin_names(self) -> list[str]:
        return [m.name for m in self._members() if m.admin and m.line_user_id]

    def _is_admin(self, chat: _Chat) -> bool:
        return is_admin(chat.settings, chat.user_id, chat.chat_id, self._admin_ids())

    def _admin_name(self, settings: Settings) -> str:
        """設定裡「出問題通知誰」那個人的名字（先看同工名單，再看 LINE 名稱）；查不到就空字串。"""
        admin = (settings.line.admin_target_id or "").strip()
        if not admin:
            return ""
        if member := next((m for m in self._members() if m.line_user_id == admin), None):
            return member.name
        return (self.service.history.person(admin) or {}).get("display_name", "")

    def _my_permissions(self, chat: _Chat) -> None:
        if not chat.user_id:
            chat.reply("抓不到你的 LINE 帳號（電腦版 LINE 不會提供），請用手機 LINE 再打一次「/我的權限」。")
            return
        person = self.service.history.person(chat.user_id) or {}
        member = next((m for m in self._members() if m.line_user_id == chat.user_id), None)
        admin = self._is_admin(chat)
        lines = ["👤 你的狀況", "",
                 f"・LINE 名稱：{chat.display_name or '（抓不到）'}",
                 f"・你的 LINE ID：{chat.user_id}"]
        if member is None:
            lines += ["・同工名單：還沒對應到你 ⚠️ 提醒不會 @ 你",
                      "　打「/我的名字 你的真實姓名」登記，管理員確認後就 @ 得到了"]
        else:
            aliases = f"（其他寫法：{'、'.join(member.aliases)}）" if member.aliases else ""
            lines.append(f"・同工名單：{member.name}{aliases}")
            lines.append("・提醒會 @ 到你 ✅")
            if not member.active:
                lines.append("・目前是「停用」狀態，被排到服事時機器人會提醒管理員")
        if claimed := (person.get("real_name") or ""):
            lines.append(f"・你登記的姓名：{claimed}（等管理員確認）")
        if nicks := nicknames_of(person):
            lines.append(f"・你登記的暱稱：{'、'.join(nicks)}（等管理員確認）")
        lines += ["", f"・身分：{'管理員 👑' if admin else '一般成員'}",
                  f"・你可以打：{EVERYONE_COMMANDS}" + (f"、{ADMIN_COMMANDS}" if admin else ""),
                  "・每個指令誰能用：打「/權限」"]
        chat.reply("\n".join(lines))

    # ------------------------------------------------------------------ /別周測試（純預覽，見 service.preview_for）

    def _test_week(self, arg: str, chat: _Chat) -> None:
        if not self._is_admin(chat):
            extra = ("請管理員來打。" if chat.settings.line.admin_target_id or self._admin_ids()
                     else "（目前還沒有管理員：到管理網頁「同工名單」把自己勾成管理員。）")
            chat.reply(f"「/別周測試」只有管理員可以用 🙏{extra}\n大家都可以打「/提醒」看這一週的服事。")
            return
        if not arg:
            chat.reply(TEST_WEEK_USAGE)
            return
        day = parse_user_date(arg, self.service.now(chat.settings).date())
        if day is None:
            chat.reply(f"看不懂日期「{arg}」🤔\n{TEST_WEEK_USAGE}")
            return
        messages, note = self.service.preview_for(day, chat.chat_id)
        if not messages:
            chat.reply(note)
            return
        chat.messenger.reply_texts(chat.reply_token, [*messages, note])

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

    def _alert_left(self, chat_id: str, messenger: LineMessenger, admin_ids: list[str]) -> None:
        targets = TargetTable(self.service.paths.targets_file)
        try:
            hit = next((t for t in targets.load().items if t.line_id == chat_id and t.enabled), None)
        except ChurchBotError:
            hit = None
        if hit is None or not admin_ids:
            return
        text = (f"⚠️ 服事提醒機器人被移出「{hit.name}」群組了，這個群組以後收不到提醒。\n"
                "如果是不小心的，請重新邀請機器人進群組；不再需要的話，到管理網頁把這個群組停用。")
        try:
            for admin_id in admin_ids:
                messenger.send(admin_id, OutgoingMessage(text=text))
        except ChurchBotError as exc:
            log.error("通知管理員失敗：%s", exc)
