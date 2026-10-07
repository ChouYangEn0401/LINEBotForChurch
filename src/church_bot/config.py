"""設定：``config/settings.yaml``（一般設定）+ ``.env``（機密）。

設計原則：
* 機密（LINE token、密碼）只存在 ``.env``，永遠不會被寫進 settings.yaml，也不會進 git。
* settings.yaml 沒有也能跑（全部用預設值），缺什麼由「健康檢查」告訴使用者。
* 設定寫錯時丟 ``ConfigError``，訊息要讓不懂 YAML 的人也看得懂。
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from church_bot.errors import ConfigError
from church_bot.files import read_text, swap, write_text

# 預設的提醒訊息：用【中文標籤】寫，不懂程式的人也改得動（規則見 core/message_template.py）
DEFAULT_TEMPLATE = """\
📣 【標題】
📅 【日期】・【聚會】

▸ 【服事名單】

📝 【備註】

【結尾】
"""

# 舊版的預設（Jinja2 寫法）。設定檔裡還是這一份的，讀進來自動換成上面那一份——排出來的字一模一樣，
# 防重複照樣認得是同一則，不會因為換寫法就多發一次。
LEGACY_DEFAULT_TEMPLATE = """\
📣 {{ title }}
📅 {{ date_text }}{% if label %}・{{ label }}{% endif %}

{% for a in assignments -%}
▸ {{ a.role }}：{{ a.names }}
{% endfor -%}
{% if note %}
📝 {{ note }}
{% endif -%}
{% if footer %}
{{ footer }}
{% endif -%}
"""

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_ZH = {
    "mon": "星期一", "tue": "星期二", "wed": "星期三", "thu": "星期四",
    "fri": "星期五", "sat": "星期六", "sun": "星期日",
}


class _Base(BaseModel):
    # extra="ignore"：舊版設定檔多了欄位也不會壞
    model_config = ConfigDict(extra="ignore", validate_assignment=True)


class ColumnAliases(_Base):
    """表頭可能的寫法。服事表的表頭只要「包含」其中一個詞就算數。"""

    date: list[str] = ["日期", "主日", "date"]
    role: list[str] = ["服事項目", "服事", "項目", "職務", "崗位", "role"]
    person: list[str] = ["服事人員", "人員", "同工", "姓名", "名字", "name"]
    label: list[str] = ["聚會", "場次", "label"]
    note: list[str] = ["備註", "說明", "note"]
    ignore: list[str] = []  # 這些欄位不要出現在訊息裡（例如「經文」「詩歌」）
    # 這些欄位照原樣顯示、不當成人名（例如「服飾」寫「深色西裝＋領帶」）。「/行事曆」顯示的就是這些欄位。
    # 預設空的：填了之後那一欄就不再出現在每週提醒的服事清單裡，提醒內容會變（排程會因此多送一次）。
    text: list[str] = []


class SourceSettings(_Base):
    kind: Literal["google_public", "google_service_account", "csv"] = "csv"
    spreadsheet_url: str = ""
    worksheet: str = ""  # 分頁名稱；空白 = 第一個分頁
    credentials_file: str = "config/service-account.json"
    csv_path: str = "config/roster.example.csv"
    layout: Literal["auto", "wide", "long", "matrix"] = "auto"
    columns: ColumnAliases = Field(default_factory=ColumnAliases)
    empty_markers: list[str] = ["-", "—", "–", "無", "休", "暫停", "x", "X", "N/A", "n/a", "/"]
    timeout_seconds: float = 20.0


class LineSettings(_Base):
    channel_access_token: str = ""  # 只從 .env 讀
    channel_secret: str = ""  # 只從 .env 讀
    admin_target_id: str = ""  # 出問題通知誰（你的 userId 或管理群組 ID）
    timeout_seconds: float = 15.0


class MessengerSettings(_Base):
    # line = 正式送到 LINE；console = 測試模式（寫到 data/outbox.log，不會真的送出）
    kind: Literal["line", "console"] = "line"
    # 每次發送前檢查本月 LINE 額度夠不夠；不夠就先警告管理員（仍會嘗試發送）
    check_quota: bool = True


class ScheduleSettings(_Base):
    enabled: bool = True
    timezone: str = "Asia/Taipei"
    day_of_week: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] = "thu"
    time: str = "20:00"
    # 每幾週發送一次：1 = 每週；2 = 每兩週發一次（大約可以省一半的 LINE 則數，見 docs/LINE_PRICING.md）
    # 用「每兩週」的話，記得把 behavior.lookahead_days 也改成至少 14，不然下一次發送前的那週服事不會出現在提醒裡
    every_n_weeks: int = Field(default=1, ge=1, le=8)

    @field_validator("time")
    @classmethod
    def _check_time(cls, v: str) -> str:
        v = v.strip().replace("：", ":")
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", v)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ValueError("時間要寫成 24 小時制，例如 20:00")
        return f"{int(m.group(1)):02d}:{m.group(2)}"

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"不認得的時區「{v}」，台灣請用 Asia/Taipei") from exc
        return v

    @property
    def hour(self) -> int:
        return int(self.time.split(":")[0])

    @property
    def minute(self) -> int:
        return int(self.time.split(":")[1])

    def describe(self) -> str:
        if not self.enabled:
            return "自動發送已關閉"
        when = f"每{WEEKDAY_ZH[self.day_of_week]}" if self.every_n_weeks == 1 else f"每 {self.every_n_weeks} 週的{WEEKDAY_ZH[self.day_of_week]}"
        return f"{when} {self.time}"


class MessageSettings(_Base):
    title: str = "本週服事提醒"
    # %Y 年 %m 月 %d 日（%-m / %-d 不補 0）；{weekday} = 週日、{weekday_long} = 星期日、{roc_year} = 民國年
    date_format: str = "%-m/%-d（{weekday}）"
    template: str = DEFAULT_TEMPLATE
    footer: str = "謝謝大家的擺上 🙏"
    name_separator: str = "、"
    # 空 = 照服事表的欄位順序；有填就照這個順序排，沒列到的排最後
    role_order: list[str] = []

    @field_validator("template")
    @classmethod
    def _upgrade_template(cls, v: str) -> str:
        return DEFAULT_TEMPLATE if v.replace("\r\n", "\n").strip() == LEGACY_DEFAULT_TEMPLATE.strip() else v


class BehaviorSettings(_Base):
    # 從「今天」往後看幾天內的聚會（含今天）。每週提醒一次的話 7 就夠了
    lookahead_days: int = Field(default=7, ge=1, le=60)
    # 已經送過同一天的提醒（包含有人先打 /提醒），排程時間到了：
    #   true  = 內容沒變就略過、服事表改過就送新的（預設，保證大家拿到的是最新的名單）
    #   false = 送過就不再送
    resend_if_changed: bool = True
    # 服事表剩不到幾天就提醒管理員「該排下一季了」
    roster_low_warning_days: int = Field(default=14, ge=0, le=365)
    # 服事表上的名字在同工名單對不到時，要不要提醒管理員
    warn_unknown_names: bool = True


class WebSettings(_Base):
    host: str = "127.0.0.1"  # 127.0.0.1 = 只有這台電腦能開；0.0.0.0 = 區網都能開
    port: int = Field(default=8787, ge=1, le=65535)
    password: str = ""  # 只從 .env 讀


class ChatSettings(_Base):
    """在 LINE 聊天室打「/」指令時的行為。"""

    # 開放大家打「/我的名字 王小明」登記真實姓名（登記後要管理員在「同工名單」頁確認才生效，亂填也不會直接影響提醒）
    collect_names: bool = True
    # 允許在 LINE 打「/設定 名稱=值」修改少數設定；每次都要輸入一次性驗證碼（見 remote_config.py）
    remote_config: bool = True
    # 驗證碼除了顯示在執行程式的畫面，也用 LINE 私訊管理員（每次算 1 則，每天最多 10 次）
    send_code_to_admin: bool = True
    # 彩蛋：設定本來就是那個值時，不糾正對方，改成順著說「好，已經幫您改好了」（見 core/easter_egg.py）
    easter_egg: bool = False


class Settings(_Base):
    source: SourceSettings = Field(default_factory=SourceSettings)
    line: LineSettings = Field(default_factory=LineSettings)
    messenger: MessengerSettings = Field(default_factory=MessengerSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    message: MessageSettings = Field(default_factory=MessageSettings)
    behavior: BehaviorSettings = Field(default_factory=BehaviorSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    chat: ChatSettings = Field(default_factory=ChatSettings)


# 機密欄位：(區段, 欄位, 環境變數)。這些值只從 .env 讀，存檔時一律排除。
SECRET_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("line", "channel_access_token", "LINE_CHANNEL_ACCESS_TOKEN"),
    ("line", "channel_secret", "LINE_CHANNEL_SECRET"),
    ("web", "password", "UI_PASSWORD"),
)


# --------------------------------------------------------------------------- paths


def _default_root() -> Path:
    env = os.environ.get("CHURCH_BOT_HOME")
    if env:
        return Path(env).expanduser().resolve()
    repo = Path(__file__).resolve().parents[2]
    if (repo / "pyproject.toml").exists():
        return repo
    return Path.cwd()


@dataclass(frozen=True, slots=True)
class Paths:
    """檔案放哪裡。``ministry`` 空白 = 整個教會（共用的東西）；有值 = 那個牧區自己的資料夾。

    牧區的設定、三張表、資料庫都在自己的資料夾裡（見 docs/MINISTRIES.md）；
    .env（LINE 金鑰、網頁密碼）、記錄檔、牧區清單 church.yaml 永遠在整個教會那一層。
    """

    root: Path
    ministry: str = ""

    @classmethod
    def discover(cls) -> "Paths":
        return cls(_default_root())

    def for_ministry(self, ministry_id: str) -> "Paths":
        return Paths(self.root, ministry_id)

    @property
    def church(self) -> "Paths":
        return Paths(self.root)

    @property
    def config_dir(self) -> Path:
        return self.root / "config" / "ministries" / self.ministry if self.ministry else self.root / "config"

    @property
    def data_dir(self) -> Path:
        return self.root / "data" / "ministries" / self.ministry if self.ministry else self.root / "data"

    @property
    def church_file(self) -> Path:
        return self.root / "config" / "church.yaml"

    @property
    def shared_db_file(self) -> Path:
        """整個教會共用的資料庫：LINE 用量、對外網址、還沒分到牧區的群組、變更紀錄。"""
        return self.root / "data" / "church_bot.db"

    @property
    def settings_file(self) -> Path:
        return self.config_dir / "settings.yaml"

    @property
    def targets_file(self) -> Path:
        return self.config_dir / "targets.csv"

    @property
    def members_file(self) -> Path:
        return self.config_dir / "members.csv"

    @property
    def teams_file(self) -> Path:
        return self.config_dir / "teams.csv"

    @property
    def env_file(self) -> Path:
        return self.root / ".env"

    @property
    def db_file(self) -> Path:
        return self.data_dir / "church_bot.db"

    @property
    def log_file(self) -> Path:
        return self.root / "data" / "church_bot.log"

    def resolve(self, p: str | Path) -> Path:
        """設定檔裡的相對路徑：牧區資料夾裡有這個檔案就用它，否則相對於專案根目錄。

        所以牧區可以把自己的服事表 CSV 放在自己的資料夾、只寫檔名；以前寫的 config/xxx.csv 也照樣找得到。
        """
        path = Path(p).expanduser()
        if path.is_absolute():
            return path
        if self.ministry and (own := self.config_dir / path).exists():
            return own
        return self.root / path


# --------------------------------------------------------------------------- .env


def read_env_file(path: Path) -> dict[str, str]:
    """極簡 .env 解析（KEY=VALUE，# 開頭是註解）。刻意不引入 python-dotenv。"""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw in read_text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        values[key] = val
    return values


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """更新 .env 裡的某些 KEY，保留其他行與註解；沒有的 KEY 補在最後。"""
    lines = read_text(path).splitlines() if path.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for raw in lines:
        key = raw.split("=", 1)[0].strip().removeprefix("export ").strip()
        if "=" in raw and not raw.lstrip().startswith("#") and key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(raw)
    out.extend(f"{k}={v}" for k, v in remaining.items())
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    swap(tmp, path)  # .env 每個網頁請求都會讀，Windows 上可能剛好被開著：等一下再試


# --------------------------------------------------------------------------- load / save


def _format_validation_error(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"])
        parts.append(f"「{loc}」{err['msg']}")
    return "；".join(parts)


def load_settings(paths: Paths) -> Settings:
    data: dict = {}
    if paths.settings_file.exists():
        try:
            loaded = yaml.safe_load(read_text(paths.settings_file))
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            where = f"第 {mark.line + 1} 行附近" if mark else ""
            raise ConfigError(
                f"設定檔 settings.yaml {where}格式錯誤",
                "通常是冒號後面少了空格、或縮排沒對齊。可以直接在網頁「設定」頁修改，比較不會出錯。",
            ) from exc
        if loaded is not None and not isinstance(loaded, dict):
            raise ConfigError(
                "設定檔 settings.yaml 內容不是正確格式",
                "請參考 config/settings.example.yaml 重寫，或直接刪掉它讓程式用預設值。",
            )
        data = loaded or {}

    # 網頁設定（host、port）是整個教會一份，放在 church.yaml；牧區自己的 settings.yaml 不管這個
    church_web = read_church_section(paths, "web")
    if church_web:
        data["web"] = {**(data.get("web") if isinstance(data.get("web"), dict) else {}), **church_web}

    # .env 的機密值覆蓋進去（系統環境變數優先於 .env 檔）
    env_values = {**read_env_file(paths.env_file), **os.environ}
    for section, key, env_name in SECRET_FIELDS:
        if env_values.get(env_name):
            section_data = data.setdefault(section, {})
            if isinstance(section_data, dict):
                section_data[key] = env_values[env_name]

    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(
            f"設定檔內容有誤：{_format_validation_error(exc)}",
            "請到網頁「設定」頁修正，或對照 config/settings.example.yaml。",
        ) from exc


def read_church_section(paths: Paths, section: str) -> dict:
    """church.yaml 裡的某一段（例如 web）。沒有檔案、壞掉、或那一段不是 dict 都回傳空 dict。"""
    if not paths.church_file.exists():
        return {}
    try:
        loaded = yaml.safe_load(read_text(paths.church_file)) or {}
    except yaml.YAMLError:
        return {}  # church.yaml 壞掉由 church.py 讀牧區清單時報錯，這裡不重複
    value = loaded.get(section) if isinstance(loaded, dict) else None
    return value if isinstance(value, dict) else {}


def settings_to_yaml_dict(settings: Settings, paths: Paths | None = None) -> dict:
    data = settings.model_dump(mode="json")
    for section, key, _ in SECRET_FIELDS:
        data.get(section, {}).pop(key, None)
    if paths is not None and paths.ministry:
        data.pop("web", None)  # 整個教會一份，在 church.yaml
    return data


SETTINGS_LOCK = threading.RLock()


def update_settings(paths: Paths, change: Callable[[Settings], None]) -> Settings:
    """讀 → 改 → 存整段排隊：管理網頁和 LINE 指令同時改設定，也不會互相蓋掉。"""
    with SETTINGS_LOCK:
        settings = load_settings(paths)
        change(settings)
        save_settings(paths, settings)
    return settings


def save_settings(paths: Paths, settings: Settings) -> None:
    """寫回 settings.yaml（不含機密）。先寫暫存檔再改名，避免寫到一半斷電變成壞檔。"""
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(
        settings_to_yaml_dict(settings, paths), allow_unicode=True, sort_keys=False, width=100
    )
    header = (
        "# 教會服事提醒機器人 — 一般設定\n"
        "# 建議用網頁「設定」頁修改；手動改的話每個欄位的說明請看 settings.example.yaml\n"
        "# 機密（LINE token / 密碼）不在這裡，在 .env\n\n"
    )
    write_text(paths.settings_file, header + body)
