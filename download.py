from __future__ import annotations

import argparse
import concurrent.futures
import email.utils
import json
import os
import random
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = PROJECT_ROOT / "config.json"
DEFAULT_UNLESS_CONFIG = PROJECT_ROOT / "unlessConfig.json"
ERROR_LOG_PATH = PROJECT_ROOT / "error.md"


def configure_console_encoding() -> None:
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


class TraditionalChineseHelpFormatter(argparse.HelpFormatter):
    def add_usage(self, usage: str | None, actions: Any, groups: Any, prefix: str | None = None) -> None:
        if prefix is None:
            prefix = "用法："
        super().add_usage(usage, actions, groups, prefix)


@dataclass(frozen=True)
class SeriesEntry:
    name: str
    url_prefix: str
    special_episodes: tuple[str, ...]
    probable_episodes: tuple[int, ...]


@dataclass(frozen=True)
class DownloadSeries:
    name: str
    url_prefix: str
    start_episode: int
    max_episode: int | None
    special_episodes: tuple[str, ...]
    probable_episodes: tuple[int, ...]


@dataclass(frozen=True)
class DownloadConfig:
    series_entries: tuple[SeriesEntry, ...]
    start_episode: int
    max_episode: int | None
    probable_episodes: tuple[int, ...]
    start_number: int
    zero_padding: int
    extensions: tuple[str, ...]
    output_dir: Path
    consecutive_404_limit: int
    consecutive_missing_episode_limit: int
    consecutive_error_limit: int
    timeout_seconds: int
    retries: int
    skip_existing: bool
    verify_downloads: bool
    stop_on_error: bool
    all_retry: int
    delay_seconds: float
    image_workers: int
    request_jitter: tuple[float, float] | None
    adaptive_extension_order: bool
    adaptive_backoff: bool
    dry_run_images: int
    dry_run_episodes: int


@dataclass(frozen=True)
class DownloadItem:
    series: DownloadSeries
    episode: int | str
    number: int
    extension: str
    source_name: str
    url: str
    target: Path


@dataclass(frozen=True)
class DownloadResult:
    status: str
    message: str
    item: DownloadItem | None = None
    error_type: str = ""
    reason: str = ""
    retryable: bool = True
    attempts: int = 0
    notice_type: str = ""
    notice_reason: str = ""


@dataclass(frozen=True)
class DownloadAttempt:
    status: str
    error_type: str = ""
    reason: str = ""
    attempts: int = 0
    notice_type: str = ""
    notice_reason: str = ""


@dataclass(frozen=True)
class EpisodeResult:
    status: str
    last_found: int
    downloaded: int
    skipped: int
    not_found: int = 0
    errors: int = 0


@dataclass
class RunStats:
    downloaded: int = 0
    skipped: int = 0
    not_found: int = 0
    errors: int = 0
    retry_rounds: int = 0


def load_config(path: Path, require_series_entries: bool = True) -> DownloadConfig:
    raw = load_merged_config(path)

    output_dir = Path(raw.get("output_dir", "download"))
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_dir

    if require_series_entries:
        url_base_prefix = str(raw.get("url_prefix", "")).strip()
        series_entries = parse_series_entries(raw["url_suffix"], url_base_prefix)
    else:
        series_entries = ()

    consecutive_404_limit = max(1, int(raw.get("consecutive_404_limit", 5)))

    config = DownloadConfig(
        series_entries=series_entries,
        start_episode=int(raw.get("start_episode", 1)),
        max_episode=parse_optional_int(raw.get("max_episode", raw.get("start_episode", 1)), "max_episode"),
        probable_episodes=parse_int_list(raw.get("probable_episode", []), "probable_episode"),
        start_number=int(raw.get("start_number", 1)),
        zero_padding=int(raw.get("zero_padding", 1)),
        extensions=parse_extensions(raw.get("extension", "jpg")),
        output_dir=output_dir,
        consecutive_404_limit=consecutive_404_limit,
        consecutive_missing_episode_limit=max(
            1,
            int(raw.get("consecutive_missing_episode_limit", consecutive_404_limit)),
        ),
        consecutive_error_limit=max(1, int(raw.get("consecutive_error_limit", 10))),
        timeout_seconds=max(1, int(raw.get("timeout_seconds", 30))),
        retries=max(1, int(raw.get("retries", 3))),
        skip_existing=bool(raw.get("skip_existing", True)),
        verify_downloads=bool(raw.get("verify_downloads", True)),
        stop_on_error=bool(raw.get("stop_on_error", False)),
        all_retry=max(0, int(raw.get("all_retry", 1))),
        delay_seconds=max(0.0, float(raw.get("delay_seconds", 0.15))),
        image_workers=parse_positive_int(raw.get("image_workers", 1), "image_workers"),
        request_jitter=parse_request_jitter(raw.get("request_jitter", [])),
        adaptive_extension_order=bool(raw.get("adaptive_extension_order", False)),
        adaptive_backoff=bool(raw.get("adaptive_backoff", False)),
        dry_run_images=max(1, int(raw.get("dry_run_images", 3))),
        dry_run_episodes=max(1, int(raw.get("dry_run_episodes", 3))),
    )

    validate_config(config, require_series_entries=require_series_entries)
    return config


def load_merged_config(path: Path) -> dict[str, Any]:
    raw = read_json_config(path)
    unless_path = unless_config_path_for(path)
    if not unless_path.exists():
        return raw

    unless_raw = read_json_config(unless_path)
    return {**unless_raw, **raw}


def unless_config_path_for(config_path: Path) -> Path:
    if config_path == DEFAULT_CONFIG:
        return DEFAULT_UNLESS_CONFIG

    sibling = config_path.parent / "unlessConfig.json"
    if sibling.exists():
        return sibling
    return DEFAULT_UNLESS_CONFIG


def read_json_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as file:
        value = json.loads(clean_json_config(file.read()))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} 最外層必須是 JSON 物件。")
    return value


def clean_json_config(text: str) -> str:
    return strip_json_trailing_commas(strip_json_comments(text))


def parse_optional_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    if isinstance(value, str):
        raise ValueError(f'{field_name} 必須是數字、null 或空字串；不要寫成 "{value}"。')
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} 必須是整數、null 或空字串。")
    return value


def parse_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} 必須是大於等於 1 的整數。")
    if value < 1:
        raise ValueError(f"{field_name} 必須是大於等於 1 的整數。")
    return value


def parse_request_jitter(value: Any) -> tuple[float, float] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("request_jitter 必須是空陣列，或兩個非負數組成的陣列。")
    if not value:
        return None
    if len(value) != 2:
        raise ValueError("request_jitter 必須是空陣列，或兩個非負數組成的陣列。")

    minimum, maximum = value
    if isinstance(minimum, bool) or isinstance(maximum, bool):
        raise ValueError("request_jitter 的兩個值必須是非負數，不能是布林值。")
    if not isinstance(minimum, (int, float)) or not isinstance(maximum, (int, float)):
        raise ValueError("request_jitter 的兩個值必須是非負數。")
    minimum = float(minimum)
    maximum = float(maximum)
    if minimum < 0 or maximum < 0:
        raise ValueError("request_jitter 的兩個值必須大於等於 0。")
    if minimum > maximum:
        raise ValueError("request_jitter 的第一個數字不可大於第二個數字。")
    return minimum, maximum


def parse_int_list(value: Any, field_name: str) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} 必須是整數陣列。")

    numbers: list[int] = []
    seen: set[int] = set()
    for index, item in enumerate(value, start=1):
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError(f"{field_name} 第 {index} 筆必須是整數。")
        if item in seen:
            continue
        numbers.append(item)
        seen.add(item)

    return tuple(sorted(numbers))


def parse_special_episodes(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field_name} 必須是陣列、null，或省略。")

    episodes: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value, start=1):
        if isinstance(item, bool):
            raise ValueError(f"{field_name} 第 {index} 筆不能是布林值。")
        episode = str(item).strip()
        if not episode:
            continue
        if episode in seen:
            continue
        episodes.append(episode)
        seen.add(episode)

    return tuple(episodes)


def parse_extensions(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        raw_extensions = value
    else:
        raw_extensions = [value]

    extensions: list[str] = []
    seen: set[str] = set()
    for raw_extension in raw_extensions:
        extension = str(raw_extension).strip().lower().lstrip(".")
        if not extension or extension in seen:
            continue
        extensions.append(extension)
        seen.add(extension)

    if not extensions:
        raise ValueError("extension 至少要有一個副檔名。")
    return tuple(extensions)


def is_valid_http_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def resolve_url_prefix(url_prefix: str, url_suffix: str) -> str:
    if not url_suffix:
        raise ValueError("url_suffix 不能是空字串。")

    if is_valid_http_url(url_suffix):
        return url_suffix

    if not url_prefix:
        raise ValueError("url_suffix 不是完整網址時，url_prefix 必須設定。")

    resolved = f"{url_prefix.rstrip('/')}/{url_suffix.lstrip('/')}"
    if not is_valid_http_url(resolved):
        raise ValueError(
            "url_suffix 不是完整網址，且與 url_prefix 組合後仍不是正確網址："
            f"{resolved}"
        )

    return resolved


def parse_series_entries(value: Any, url_prefix: str) -> tuple[SeriesEntry, ...]:
    if not isinstance(value, list):
        raise ValueError('url_suffix 必須是陣列，格式為 [{"name": "", "url": "..."}]。')

    entries: list[SeriesEntry] = []
    seen_names: set[str] = set()
    for index, raw_entry in enumerate(value, start=1):
        if not isinstance(raw_entry, dict):
            raise ValueError(f'url_suffix 第 {index} 筆必須是物件，格式為 {{"name": "", "url": "..."}}。')

        raw_url = str(raw_entry.get("url", "")).strip()
        resolved_url = resolve_url_prefix(url_prefix, raw_url)
        name = sanitize_series_name(str(raw_entry.get("name", "")).strip())
        if not name:
            name = series_name_from_url(resolved_url)
        special_episodes = parse_special_episodes(
            raw_entry.get("special_episode"),
            f"url_suffix 第 {index} 筆 special_episode",
        )
        probable_episodes = parse_int_list(
            raw_entry.get("probable_episode", []),
            f"url_suffix 第 {index} 筆 probable_episode",
        )

        if name in seen_names:
            raise ValueError(f"url_suffix 的作品資料夾名稱重複：{name}")

        entries.append(
            SeriesEntry(
                name=name,
                url_prefix=resolved_url,
                special_episodes=special_episodes,
                probable_episodes=probable_episodes,
            )
        )
        seen_names.add(name)

    if not entries:
        raise ValueError("url_suffix 至少要有一個作品設定。")

    return tuple(entries)


class ErrorLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.run_started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.records: list[dict[str, Any]] = []
        self.next_number = 1
        self.next_notice_number = 1

    def reset(self) -> None:
        self.records = []
        self.next_number = 1
        self.next_notice_number = 1
        if self.path.exists():
            self.path.unlink()

    def load(self) -> None:
        self.records = self.read_records(self.path)
        numbers: list[int] = []
        notice_numbers: list[int] = []
        for record in self.records:
            record_id = str(record.get("id", ""))
            match = re.fullmatch(r"ERROR-(\d+)", record_id)
            if match:
                numbers.append(int(match.group(1)))
                continue
            notice_match = re.fullmatch(r"NOTICE-(\d+)", record_id)
            if notice_match:
                notice_numbers.append(int(notice_match.group(1)))
        self.next_number = (max(numbers) + 1) if numbers else 1
        self.next_notice_number = (max(notice_numbers) + 1) if notice_numbers else 1

    def add(self, record: dict[str, Any]) -> None:
        if not record.get("id"):
            record["id"] = self.next_id()
        self.records.append(record)
        self.write()

    def add_notice(self, record: dict[str, Any]) -> None:
        if not record.get("id"):
            record["id"] = self.next_notice_id()
        record["record_type"] = "notice"
        record["retryable"] = False
        self.records.append(record)
        self.write()

    def add_item_error(
        self,
        item: DownloadItem,
        error_type: str,
        reason: str,
        attempts: int,
        retryable: bool = True,
    ) -> None:
        self.add(
            {
                "record_type": "error",
                "error_type": error_type,
                "reason": reason,
                "retryable": retryable,
                "url": item.url,
                "target": str(item.target.resolve()),
                "series_name": item.series.name,
                "episode": item.episode,
                "number": item.number,
                "source_name": item.source_name,
                "target_name": item.target.name,
                "extension": item.extension,
                "attempts": attempts,
            }
        )

    def add_episode_error(
        self,
        config: DownloadConfig,
        series: DownloadSeries,
        episode: int | str,
        error_type: str,
        reason: str,
    ) -> None:
        self.add(
            {
                "record_type": "error",
                "error_type": error_type,
                "reason": reason,
                "retryable": False,
                "url": series.url_prefix.rstrip("/") + f"/{episode}/",
                "target": str((series_root(config, series) / str(episode)).resolve()),
                "series_name": series.name,
                "episode": episode,
                "number": None,
                "source_name": "",
                "target_name": "",
                "extension": "",
                "attempts": 0,
            }
        )

    def add_item_notice(
        self,
        item: DownloadItem,
        notice_type: str,
        reason: str,
        attempts: int,
    ) -> None:
        self.add_notice(
            {
                "notice_type": notice_type,
                "reason": reason,
                "url": item.url,
                "target": str(item.target.resolve()),
                "series_name": item.series.name,
                "episode": item.episode,
                "number": item.number,
                "source_name": item.source_name,
                "target_name": item.target.name,
                "extension": item.extension,
                "attempts": attempts,
            }
        )

    def next_id(self) -> str:
        record_id = f"ERROR-{self.next_number:04d}"
        self.next_number += 1
        return record_id

    def next_notice_id(self) -> str:
        record_id = f"NOTICE-{self.next_notice_number:04d}"
        self.next_notice_number += 1
        return record_id

    def write(self) -> None:
        if not self.records:
            if self.path.exists():
                self.path.unlink()
            return

        lines = [
            "# Download Errors And Notices",
            "",
            f"Run started: {self.run_started}",
            "",
        ]
        for record in self.records:
            lines.append(f"## {record['id']}")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(record, ensure_ascii=False, indent=2))
            lines.append("```")
            lines.append("")

        temp_path = self.path.with_name(self.path.name + ".tmp")
        temp_path.write_text("\n".join(lines), encoding="utf-8")
        temp_path.replace(self.path)

    def has_retryable_records(self) -> bool:
        return any(bool(record.get("retryable")) for record in self.records)

    def error_records(self) -> list[dict[str, Any]]:
        return [record for record in self.records if str(record.get("record_type", "error")) != "notice"]

    def notice_records(self) -> list[dict[str, Any]]:
        return [record for record in self.records if str(record.get("record_type", "error")) == "notice"]

    @staticmethod
    def read_records(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []

        text = path.read_text(encoding="utf-8-sig")
        records: list[dict[str, Any]] = []
        pattern = re.compile(r"##\s+((?:ERROR|NOTICE)-\d+)\s+```json\s+(.*?)\s+```", re.DOTALL)
        for match in pattern.finditer(text):
            record_id = match.group(1)
            try:
                record = json.loads(match.group(2))
            except json.JSONDecodeError:
                continue
            record.setdefault("id", record_id)
            if record_id.startswith("NOTICE-"):
                record.setdefault("record_type", "notice")
                record.setdefault("retryable", False)
            else:
                record.setdefault("record_type", "error")
            records.append(record)

        return records


def strip_json_comments(text: str) -> str:
    result: list[str] = []
    in_string = False
    escaped = False
    index = 0

    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""

        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue

        if char == "/" and next_char == "/":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue

        if char == "/" and next_char == "*":
            index += 2
            while index + 1 < len(text) and not (text[index] == "*" and text[index + 1] == "/"):
                result.append("\n" if text[index] in "\r\n" else " ")
                index += 1
            index += 2
            continue

        result.append(char)
        index += 1

    return "".join(result)


def strip_json_trailing_commas(text: str) -> str:
    result: list[str] = []
    in_string = False
    escaped = False
    index = 0

    while index < len(text):
        char = text[index]

        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue

        if char == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "]}":
                index += 1
                continue

        result.append(char)
        index += 1

    return "".join(result)


def validate_config(config: DownloadConfig, require_series_entries: bool = True) -> None:
    if require_series_entries and not config.series_entries:
        raise ValueError("url_suffix 至少要有一個作品設定。")
    for entry in config.series_entries:
        if not is_valid_http_url(entry.url_prefix):
            raise ValueError("解析後的網址必須是 http:// 或 https:// 開頭的正確網址。")
        if any(episode < 1 for episode in entry.probable_episodes):
            raise ValueError(f"url_suffix 作品 {entry.name} 的 probable_episode 每個集數都必須大於等於 1。")
    if config.start_episode < 1:
        raise ValueError("start_episode 必須大於等於 1。")
    if config.max_episode is not None and config.max_episode < config.start_episode:
        raise ValueError("max_episode 必須大於等於 start_episode。")
    if any(episode < 1 for episode in config.probable_episodes):
        raise ValueError("probable_episode 的每個集數都必須大於等於 1。")
    if config.start_number < 0:
        raise ValueError("start_number 必須大於等於 0。")
    if config.zero_padding < 1:
        raise ValueError("zero_padding 必須大於等於 1。")
    if config.consecutive_error_limit < 1:
        raise ValueError("consecutive_error_limit 必須大於等於 1。")
    if config.all_retry < 0:
        raise ValueError("all_retry 必須大於等於 0。")
    if config.image_workers < 1:
        raise ValueError("image_workers 必須大於等於 1。")
    if not config.extensions:
        raise ValueError("extension 至少要有一個副檔名。")


BACKOFF_HTTP_STATUS_CODES = {403, 429, 500, 502, 503, 504}


class RequestThrottler:
    def __init__(
        self,
        delay_seconds: float,
        request_jitter: tuple[float, float] | None,
        adaptive_backoff: bool,
    ) -> None:
        self.delay_seconds = delay_seconds
        self.request_jitter = request_jitter
        self.adaptive_backoff = adaptive_backoff
        self.lock = threading.Lock()
        self.next_request_time = 0.0
        self.backoff_until = 0.0
        self.backoff_hits = 0

    def wait_before_request(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                wait_until = max(self.next_request_time, self.backoff_until)
                if now >= wait_until:
                    interval = self.next_interval()
                    self.next_request_time = time.monotonic() + interval
                    return

                wait_seconds = wait_until - now

            time.sleep(wait_seconds)

    def next_interval(self) -> float:
        if self.request_jitter is not None:
            minimum, maximum = self.request_jitter
            return random.uniform(minimum, maximum)
        return self.delay_seconds

    def record_success(self) -> None:
        if not self.adaptive_backoff:
            return
        with self.lock:
            if time.monotonic() >= self.backoff_until:
                self.backoff_hits = 0

    def record_exception(self, exc: Exception) -> None:
        if not self.adaptive_backoff:
            return

        should_backoff, retry_after, label = backoff_details(exc)
        if not should_backoff:
            return

        with self.lock:
            seconds = retry_after if retry_after is not None else (30.0 if self.backoff_hits == 0 else 60.0)
            self.backoff_hits += 1
            self.backoff_until = max(self.backoff_until, time.monotonic() + seconds)
            print(f"觸發降速：{label}，暫停 {seconds:.1f} 秒後繼續。")


def backoff_details(exc: Exception) -> tuple[bool, float | None, str]:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 404:
            return False, None, "HTTP 404"
        if exc.code in BACKOFF_HTTP_STATUS_CODES:
            return True, parse_retry_after(exc.headers), f"HTTP {exc.code}"
        return False, None, f"HTTP {exc.code}"

    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if is_transient_network_error(reason):
            return True, None, f"url_error: {reason}"
        return False, None, f"url_error: {reason}"

    if is_transient_network_error(exc):
        return True, None, exc.__class__.__name__
    return False, None, exc.__class__.__name__


def is_transient_network_error(value: Any) -> bool:
    if isinstance(value, (TimeoutError, socket.timeout, ConnectionResetError, ssl.SSLError)):
        return True

    text = str(value).lower()
    return any(
        pattern in text
        for pattern in (
            "timed out",
            "timeout",
            "connection reset",
            "temporarily unavailable",
            "ssl",
        )
    )


def parse_retry_after(headers: Any) -> float | None:
    if not headers:
        return None

    value = headers.get("Retry-After")
    if not value:
        return None

    value = str(value).strip()
    if not value:
        return None
    if value.isdigit():
        return max(0.0, float(value))

    try:
        retry_time = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None

    if retry_time.tzinfo is None:
        return None
    return max(0.0, retry_time.timestamp() - time.time())


def build_series_list(config: DownloadConfig) -> list[DownloadSeries]:
    return [
        DownloadSeries(
            name=entry.name,
            url_prefix=entry.url_prefix,
            start_episode=config.start_episode,
            max_episode=config.max_episode,
            special_episodes=entry.special_episodes,
            probable_episodes=merge_int_tuples(config.probable_episodes, entry.probable_episodes),
        )
        for entry in config.series_entries
    ]


def merge_int_tuples(*values: tuple[int, ...]) -> tuple[int, ...]:
    merged: list[int] = []
    seen: set[int] = set()
    for group in values:
        for value in group:
            if value in seen:
                continue
            merged.append(value)
            seen.add(value)
    return tuple(sorted(merged))


def sanitize_series_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")


def series_name_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parsed.path).strip("/")
    name = path.split("/")[-1] if path else parsed.netloc
    name = sanitize_series_name(name)
    return name or "series"


def series_root(config: DownloadConfig, series: DownloadSeries) -> Path:
    return config.output_dir / series.name


def build_item(
    config: DownloadConfig,
    series: DownloadSeries,
    episode: int | str,
    number: int,
    extension: str,
    source_stem: str | None = None,
) -> DownloadItem:
    prefix = series.url_prefix
    if not prefix.endswith("/"):
        prefix += "/"

    target_stem = f"{number:0{config.zero_padding}d}"
    target_filename = f"{target_stem}.{extension}"
    source_filename = f"{source_stem if source_stem is not None else target_stem}.{extension}"
    episode_name = str(episode)
    return DownloadItem(
        series=series,
        episode=episode,
        number=number,
        extension=extension,
        source_name=source_filename,
        url=f"{prefix}{episode_name}/{source_filename}",
        target=series_root(config, series) / episode_name / target_filename,
    )


def candidate_number_stems(number: int, zero_padding: int) -> tuple[str, ...]:
    raw_stem = str(number)
    max_padded_stem = f"{number:0{zero_padding}d}"
    stems = [max_padded_stem, raw_stem]

    for width in range(2, zero_padding):
        stems.append(f"{number:0{width}d}")

    deduped: list[str] = []
    seen: set[str] = set()
    for stem in stems:
        if stem in seen:
            continue
        deduped.append(stem)
        seen.add(stem)

    return tuple(deduped)


def build_candidate_items(
    config: DownloadConfig,
    series: DownloadSeries,
    episode: int | str,
    number: int,
    extension_order: tuple[str, ...] | None = None,
) -> list[DownloadItem]:
    items: list[DownloadItem] = []
    extensions = extension_order if extension_order is not None else config.extensions
    for source_stem in candidate_number_stems(number, config.zero_padding):
        for extension in extensions:
            items.append(build_item(config, series, episode, number, extension, source_stem))
    return items


def extension_order_for(config: DownloadConfig, preferred_extension: str | None) -> tuple[str, ...]:
    if not config.adaptive_extension_order or not preferred_extension:
        return config.extensions
    if preferred_extension not in config.extensions:
        return config.extensions
    return (preferred_extension,) + tuple(extension for extension in config.extensions if extension != preferred_extension)


def item_label(item: DownloadItem) -> str:
    return f"[{item.series.name}/{item.episode}]"


def item_target_label(item: DownloadItem) -> str:
    if item.source_name == item.target.name:
        return item.target.name
    return f"{item.target.name}（來源：{item.source_name}）"


def episode_label(series: DownloadSeries, episode: int | str) -> str:
    return f"{series.name}/{episode}"


def validate_image_file(path: Path, extension: str) -> tuple[bool, str]:
    try:
        size = path.stat().st_size
        if size <= 0:
            return False, "空檔"

        with path.open("rb") as file:
            head = file.read(32)
            file.seek(max(0, size - 64))
            tail = file.read(64)
    except OSError as exc:
        return False, str(exc)

    if head.startswith(b"\xff\xd8"):
        if b"\xff\xd9" not in tail:
            return True, "有效 JPEG（缺少結尾標記但已放寬接受）"
        return True, "有效 JPEG"

    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        if b"IEND" not in tail:
            return False, "缺少 PNG IEND 標記"
        return True, "有效 PNG"

    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        riff_size = int.from_bytes(head[4:8], "little") + 8
        if riff_size != size:
            return False, "WEBP 檔案大小不一致"
        return True, "有效 WEBP"

    if head.startswith((b"GIF87a", b"GIF89a")):
        if not tail.endswith(b";"):
            return False, "缺少 GIF 結尾標記"
        return True, "有效 GIF"

    ext = extension.lower().lstrip(".")
    if ext in {"jpg", "jpeg", "png", "webp", "gif"}:
        return False, "無法辨識圖片格式"

    return True, "僅通過非空檔檢查"


def validation_notice_type(reason: str) -> str:
    if "缺少結尾標記但已放寬接受" in reason:
        return "jpeg_missing_end_marker"
    return ""


def request_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        )
    }


def error_details(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}", str(exc.reason or exc)
    if isinstance(exc, urllib.error.URLError):
        return "url_error", str(exc.reason)
    return exc.__class__.__name__, str(exc)


def download_url_to_path(
    url: str,
    target: Path,
    extension: str,
    config: DownloadConfig,
    throttler: RequestThrottler,
) -> DownloadAttempt:
    last_error_type = ""
    last_reason = ""
    temp_target = target.with_suffix(target.suffix + ".part")

    for attempt in range(1, config.retries + 1):
        try:
            request = urllib.request.Request(url, headers=request_headers())
            throttler.wait_before_request()
            with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
                status = getattr(response, "status", 200)
                if status >= 400:
                    raise urllib.error.HTTPError(
                        url,
                        status,
                        f"HTTP {status}",
                        response.headers,
                        None,
                    )

                target.parent.mkdir(parents=True, exist_ok=True)
                with temp_target.open("wb") as file:
                    while True:
                        chunk = response.read(1024 * 128)
                        if not chunk:
                            break
                        file.write(chunk)

            temp_target.replace(target)
            throttler.record_success()
            notice_type = ""
            notice_reason = ""
            if config.verify_downloads:
                valid, reason = validate_image_file(target, extension)
                if not valid:
                    last_error_type = "image_validation_failed"
                    last_reason = f"圖片檢查失敗：{reason}"
                    if target.exists():
                        target.unlink()
                    if attempt < config.retries:
                        time.sleep(min(2 * attempt, 5))
                        continue
                    return DownloadAttempt("error", last_error_type, last_reason, attempt)
                notice_type = validation_notice_type(reason)
                if notice_type:
                    notice_reason = reason

            return DownloadAttempt(
                "ok",
                attempts=attempt,
                notice_type=notice_type,
                notice_reason=notice_reason,
            )
        except urllib.error.HTTPError as exc:
            if temp_target.exists():
                temp_target.unlink()
            if exc.code == 404:
                return DownloadAttempt("not_found", "HTTP 404", str(exc.reason or "Not Found"), attempt)
            throttler.record_exception(exc)
            last_error_type, last_reason = error_details(exc)
            if attempt < config.retries:
                time.sleep(min(2 * attempt, 5))
        except Exception as exc:
            if temp_target.exists():
                temp_target.unlink()
            throttler.record_exception(exc)
            last_error_type, last_reason = error_details(exc)
            if attempt < config.retries:
                time.sleep(min(2 * attempt, 5))

    return DownloadAttempt("error", last_error_type, last_reason, config.retries)


def download_item(item: DownloadItem, config: DownloadConfig, throttler: RequestThrottler) -> DownloadResult:
    label = item_label(item)

    if config.skip_existing and item.target.exists() and item.target.stat().st_size > 0:
        if not config.verify_downloads:
            return DownloadResult("skip", f"略過 {label} {item.target.name}", item=item)

        valid, _reason = validate_image_file(item.target, item.extension)
        if valid:
            return DownloadResult("skip", f"略過 {label} {item.target.name}", item=item)

    attempt = download_url_to_path(item.url, item.target, item.extension, config, throttler)

    if attempt.status == "ok":
        return DownloadResult(
            "ok",
            f"完成 {label} {item_target_label(item)}",
            item=item,
            attempts=attempt.attempts,
            notice_type=attempt.notice_type,
            notice_reason=attempt.notice_reason,
        )

    if attempt.status == "not_found":
        return DownloadResult(
            "not_found",
            f"404  {label} {item.source_name}",
            item=item,
            error_type=attempt.error_type,
            reason=attempt.reason,
            retryable=False,
            attempts=attempt.attempts,
        )

    return DownloadResult(
        "error",
        f"錯誤 {label} {item_target_label(item)} - {attempt.error_type}: {attempt.reason}",
        item=item,
        error_type=attempt.error_type,
        reason=attempt.reason,
        retryable=True,
        attempts=attempt.attempts,
    )


def download_number(
    config: DownloadConfig,
    series: DownloadSeries,
    episode: int | str,
    number: int,
    throttler: RequestThrottler,
    extension_order: tuple[str, ...] | None = None,
) -> DownloadResult:
    not_found_names: list[str] = []
    candidate_items = build_candidate_items(config, series, episode, number, extension_order)

    if config.skip_existing:
        for item in candidate_items:
            if not item.target.exists() or item.target.stat().st_size <= 0:
                continue
            if not config.verify_downloads:
                return DownloadResult("skip", f"略過 {item_label(item)} {item.target.name}", item=item)
            valid, _reason = validate_image_file(item.target, item.extension)
            if valid:
                return DownloadResult("skip", f"略過 {item_label(item)} {item.target.name}", item=item)

    for item in candidate_items:
        result = download_item(item, config, throttler)

        if result.status == "not_found":
            not_found_names.append(item.source_name)
            continue

        if not_found_names and result.status in {"ok", "skip"}:
            return DownloadResult(
                result.status,
                f"{result.message}（前面找不到：{', '.join(not_found_names)}）",
                item=result.item,
                error_type=result.error_type,
                reason=result.reason,
                retryable=result.retryable,
                attempts=result.attempts,
                notice_type=result.notice_type,
                notice_reason=result.notice_reason,
            )

        return result

    first_extension = (extension_order or config.extensions)[0]
    first_item = build_item(config, series, episode, number, first_extension)
    return DownloadResult(
        "not_found",
        f"404  {item_label(first_item)} " + " / ".join(not_found_names),
        item=first_item,
        error_type="HTTP 404",
        reason="所有候選 URL 都回傳 404",
        retryable=False,
    )


def check_episode(
    config: DownloadConfig,
    series: DownloadSeries,
    episode: int | str,
    expected_last_number: int | None = None,
) -> DownloadResult:
    label = episode_label(series, episode)
    episode_dir = series_root(config, series) / str(episode)
    if not episode_dir.exists():
        return DownloadResult("check_fail", f"檢查 [{label}] 失敗 - 資料夾不存在：{episode_dir}")

    extensions = set(config.extensions)
    numbered_files: dict[int, list[Path]] = {}
    for path in episode_dir.iterdir():
        if not path.is_file():
            continue
        if path.suffix.lower().lstrip(".") not in extensions:
            continue
        if not path.stem.isdigit():
            continue
        numbered_files.setdefault(int(path.stem), []).append(path)

    highest_number = max(numbered_files) if numbered_files else config.start_number - 1
    if expected_last_number is None:
        expected_last_number = highest_number

    if expected_last_number < config.start_number:
        if numbered_files:
            return DownloadResult(
                "check_fail",
                f"檢查 [{label}] 失敗 - 找到檔案，但沒有記錄預期的最後編號。",
            )
        return DownloadResult("check_ok", f"檢查 [{label}] 正常 - 沒有找到圖片。")

    expected_start_number = config.start_number
    if config.start_number == 0 and 0 not in numbered_files and expected_last_number >= 1:
        expected_start_number = 1

    expected_numbers = set(range(expected_start_number, expected_last_number + 1))
    expected_count = len(expected_numbers)
    actual_count = sum(len(paths) for paths in numbered_files.values())

    missing_numbers = [
        number
        for number in sorted(expected_numbers)
        if not any(
            build_item(config, series, episode, number, extension).target.exists()
            for extension in config.extensions
        )
    ]
    extra_numbers = sorted(number for number in numbered_files if number not in expected_numbers)
    duplicate_numbers = sorted(number for number, paths in numbered_files.items() if len(paths) > 1)

    broken_files: list[str] = []
    for number in sorted(expected_numbers):
        for path in numbered_files.get(number, []):
            valid, reason = validate_image_file(path, path.suffix.lstrip("."))
            if not valid:
                broken_files.append(f"{path.name} ({reason})")

    problems: list[str] = []
    if highest_number != expected_last_number:
        problems.append(f"最後檔名編號是 {highest_number}，預期 {expected_last_number}")
    if actual_count != expected_count:
        problems.append(f"檔案數量是 {actual_count}，預期 {expected_count}")
    if missing_numbers:
        problems.append("缺少 " + ", ".join(str(number) for number in missing_numbers[:20]))
    if extra_numbers:
        problems.append("多出 " + ", ".join(str(number) for number in extra_numbers[:20]))
    if duplicate_numbers:
        problems.append("重複 " + ", ".join(str(number) for number in duplicate_numbers[:20]))
    if broken_files:
        problems.append("壞檔 " + ", ".join(broken_files[:10]))

    if problems:
        return DownloadResult(
            "check_fail",
            f"檢查 [{label}] 失敗 - " + "; ".join(problems),
        )

    return DownloadResult(
        "check_ok",
        f"檢查 [{label}] 正常 - {actual_count} 個檔案，最後編號 {highest_number}。",
    )


def existing_episode_numbers(config: DownloadConfig, series: DownloadSeries) -> list[int]:
    root = series_root(config, series)
    if series.max_episode is not None:
        return list(range(series.start_episode, series.max_episode + 1))
    if not root.exists():
        return []
    return sorted(int(path.name) for path in root.iterdir() if path.is_dir() and path.name.isdigit())


def episode_has_existing_download(config: DownloadConfig, series: DownloadSeries, episode: int | str) -> bool:
    if not config.skip_existing:
        return False

    episode_dir = series_root(config, series) / str(episode)
    if not episode_dir.exists() or not episode_dir.is_dir():
        return False

    extensions = set(config.extensions)
    for path in episode_dir.iterdir():
        if not path.is_file():
            continue
        if path.suffix.lower().lstrip(".") not in extensions:
            continue
        try:
            if path.stat().st_size <= 0:
                continue
        except OSError:
            continue
        if not config.verify_downloads:
            return True

        valid, _reason = validate_image_file(path, path.suffix.lstrip("."))
        if valid:
            return True

    return False


def next_probable_episode_after(
    series: DownloadSeries,
    episode: int,
    probable_index: int,
) -> tuple[int | None, int]:
    while (
        probable_index < len(series.probable_episodes)
        and series.probable_episodes[probable_index] <= episode
    ):
        probable_index += 1

    if probable_index >= len(series.probable_episodes):
        return None, probable_index

    next_episode = series.probable_episodes[probable_index]
    return next_episode, probable_index + 1


def check_existing_downloads(config: DownloadConfig) -> int:
    failed = 0
    for series in build_series_list(config):
        episodes = existing_episode_numbers(config, series)
        if not episodes:
            print(f"檢查 [{series.name}] 略過 - 沒有既有集數資料夾。")
            continue
        for episode in episodes:
            result = check_episode(config, series, episode)
            print(result.message)
            if result.status == "check_fail":
                failed += 1

    if failed:
        print(f"檢查完成，有 {failed} 個集數未通過。")
        return 1

    print("檢查完成。")
    return 0


def preview_plan(config: DownloadConfig) -> int:
    for series in build_series_list(config):
        for episode in series.special_episodes:
            for number in range(config.start_number, config.start_number + config.dry_run_images):
                for item in build_candidate_items(config, series, episode, number):
                    print(f"{item.url} -> {item.target}")

        if series.max_episode is None:
            stop_episode = series.start_episode + config.dry_run_episodes - 1
        else:
            stop_episode = series.max_episode

        for episode in range(series.start_episode, stop_episode + 1):
            for number in range(config.start_number, config.start_number + config.dry_run_images):
                for item in build_candidate_items(config, series, episode, number):
                    print(f"{item.url} -> {item.target}")

    print(
        "預覽完成。自動偵測集數與圖片只會顯示範例；"
        f"每集圖片會在連續 {config.consecutive_404_limit} 個 404 後停止，"
        f"自動集數會在連續 {config.consecutive_missing_episode_limit} 個缺集後停止。"
    )
    return 0


def download_episode(
    config: DownloadConfig,
    series: DownloadSeries,
    episode: int | str,
    error_logger: ErrorLogger,
    throttler: RequestThrottler,
    verify_after_download: bool = True,
) -> EpisodeResult:
    print("")
    print(f"集數 {episode_label(series, episode)}")

    consecutive_404 = 0
    consecutive_error = 0
    downloaded = 0
    skipped = 0
    not_found = 0
    errors = 0
    last_found = config.start_number - 1
    next_number = config.start_number
    expected_number = config.start_number
    preferred_extension: str | None = None
    stop_dispatch = False
    error_skip = False
    workers = 1 if config.stop_on_error else config.image_workers

    pending: dict[concurrent.futures.Future[DownloadResult], int] = {}
    completed: dict[int, DownloadResult] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        while pending or completed or not stop_dispatch:
            while not stop_dispatch:
                unprocessed = len(pending) + len(completed)
                available_worker_slots = workers - len(pending)
                available_404_slots = config.consecutive_404_limit - consecutive_404 - unprocessed
                available_error_slots = config.consecutive_error_limit - consecutive_error - unprocessed
                available_slots = min(available_worker_slots, available_404_slots, available_error_slots)
                if available_slots <= 0:
                    break

                extension_order = extension_order_for(config, preferred_extension)
                future = executor.submit(
                    download_number,
                    config,
                    series,
                    episode,
                    next_number,
                    throttler,
                    extension_order,
                )
                pending[future] = next_number
                next_number += 1

            if expected_number not in completed and pending:
                done, _pending = concurrent.futures.wait(
                    pending,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for future in done:
                    number = pending.pop(future)
                    try:
                        completed[number] = future.result()
                    except Exception as exc:
                        item = build_item(config, series, episode, number, config.extensions[0])
                        error_type, reason = error_details(exc)
                        completed[number] = DownloadResult(
                            "error",
                            f"錯誤 {item_label(item)} {item.target.name} - {error_type}: {reason}",
                            item=item,
                            error_type=error_type,
                            reason=reason,
                            retryable=True,
                            attempts=0,
                        )
            elif expected_number not in completed:
                break

            while expected_number in completed:
                number = expected_number
                result = completed.pop(number)
                print(result.message)

                if result.status == "not_found":
                    consecutive_404 += 1
                    consecutive_error = 0
                    not_found += 1
                    if consecutive_404 >= config.consecutive_404_limit:
                        stop_dispatch = True
                elif result.status in {"ok", "skip"}:
                    consecutive_404 = 0
                    consecutive_error = 0
                    last_found = number
                    if result.item is not None and config.adaptive_extension_order:
                        preferred_extension = result.item.extension
                    if result.status == "ok" and result.item is not None and result.notice_type:
                        print(f"提醒 {item_label(result.item)} {result.item.target.name} - {result.notice_reason}")
                        error_logger.add_item_notice(
                            result.item,
                            result.notice_type,
                            result.notice_reason,
                            result.attempts,
                        )
                    if result.status == "ok":
                        downloaded += 1
                    else:
                        skipped += 1
                else:
                    consecutive_404 = 0
                    consecutive_error += 1
                    errors += 1
                    if result.item is not None:
                        error_logger.add_item_error(
                            result.item,
                            result.error_type or "download_error",
                            result.reason or result.message,
                            result.attempts,
                            retryable=result.retryable,
                        )
                    else:
                        error_logger.add_episode_error(
                            config,
                            series,
                            episode,
                            "download_error",
                            result.reason or result.message,
                        )

                    if config.stop_on_error or consecutive_error >= config.consecutive_error_limit:
                        error_type = "stop_on_error" if config.stop_on_error else "consecutive_error_limit"
                        if config.stop_on_error:
                            reason = f"stop_on_error 已啟用，已跳過集數 {episode_label(series, episode)}。"
                        else:
                            reason = (
                                f"連續 {consecutive_error} 個非 404 錯誤，"
                                f"已跳過集數 {episode_label(series, episode)}。"
                            )
                        print(reason)
                        error_logger.add_episode_error(
                            config,
                            series,
                            episode,
                            error_type,
                            reason,
                        )
                        stop_dispatch = True
                        error_skip = True

                expected_number += 1

    if error_skip:
        return EpisodeResult("error_skip", last_found, downloaded, skipped, not_found, errors)

    if last_found < config.start_number:
        print(
            f"集數 {episode_label(series, episode)} 判定不存在："
            f"連續 {config.consecutive_404_limit} 個圖片編號都回傳 404。"
        )
        return EpisodeResult("missing", last_found, downloaded, skipped, not_found, errors)

    print(
        f"集數 {episode_label(series, episode)} 完成。最後圖片編號：{last_found}；"
        f"新下載：{downloaded}；略過：{skipped}。"
    )

    if config.verify_downloads and verify_after_download:
        check_result = check_episode(config, series, episode, last_found)
        print(check_result.message)
        if check_result.status == "check_fail":
            errors += 1
            error_logger.add_episode_error(
                config,
                series,
                episode,
                "check_failed",
                check_result.message,
            )

    return EpisodeResult("found", last_found, downloaded, skipped, not_found, errors)


def update_stats(stats: RunStats, result: EpisodeResult) -> None:
    stats.downloaded += result.downloaded
    stats.skipped += result.skipped
    stats.not_found += result.not_found
    stats.errors += result.errors


def run_special_episodes(
    config: DownloadConfig,
    series: DownloadSeries,
    error_logger: ErrorLogger,
    stats: RunStats,
    throttler: RequestThrottler,
) -> None:
    if not series.special_episodes:
        return

    print("特殊集數：" + ", ".join(series.special_episodes))
    for episode in series.special_episodes:
        result = download_episode(config, series, episode, error_logger, throttler)
        update_stats(stats, result)


def run_fixed_series(
    config: DownloadConfig,
    series: DownloadSeries,
    error_logger: ErrorLogger,
    stats: RunStats,
    throttler: RequestThrottler,
) -> int:
    assert series.max_episode is not None
    for episode in range(series.start_episode, series.max_episode + 1):
        result = download_episode(config, series, episode, error_logger, throttler)
        update_stats(stats, result)
    return 0


def run_auto_series(
    config: DownloadConfig,
    series: DownloadSeries,
    error_logger: ErrorLogger,
    stats: RunStats,
    throttler: RequestThrottler,
) -> int:
    consecutive_missing = 0
    episode = series.start_episode
    last_found_episode = 0
    probable_index = 0
    jumped_from_probable = False

    while True:
        if jumped_from_probable:
            jumped_from_probable = False
            if episode_has_existing_download(config, series, episode):
                print(f"{series.name} 起跳點 {episode} 已有既有下載，改找下一個起跳點。")
                next_episode, probable_index = next_probable_episode_after(series, episode, probable_index)
                if next_episode is None:
                    break
                print(f"{series.name} 改從可能集數 {next_episode} 繼續偵測。")
                episode = next_episode
                jumped_from_probable = True
                consecutive_missing = 0
                continue

        result = download_episode(config, series, episode, error_logger, throttler)
        update_stats(stats, result)
        if result.status in {"missing", "error_skip"}:
            consecutive_missing += 1
            print(
                f"{series.name} 連續缺集或錯誤跳過計數："
                f"{consecutive_missing}/{config.consecutive_missing_episode_limit}"
            )
            if consecutive_missing >= config.consecutive_missing_episode_limit:
                next_episode, probable_index = next_probable_episode_after(series, episode, probable_index)
                if next_episode is None:
                    break

                print(
                    f"{series.name} 改從可能集數 {next_episode} 繼續偵測；"
                    f"已略過 {config.consecutive_missing_episode_limit} 個連續缺集或錯誤集數。"
                )
                episode = next_episode
                jumped_from_probable = True
                consecutive_missing = 0
                continue
        else:
            consecutive_missing = 0
            last_found_episode = episode
        episode += 1

    print(
        f"作品 {series.name} 完成。最後找到集數：{last_found_episode}；"
        f"因連續 {config.consecutive_missing_episode_limit} 個缺集且沒有可用跳點而停止。"
    )
    return 0


def retry_error_record(config: DownloadConfig, record: dict[str, Any], throttler: RequestThrottler) -> bool:
    url = str(record.get("url", "")).strip()
    target_value = str(record.get("target", "")).strip()
    extension = str(record.get("extension", "")).strip().lstrip(".")

    if not url or not target_value or not extension:
        record["reason"] = "缺少 url、target 或 extension，無法自動補抓。"
        record["retryable"] = False
        return False

    target = Path(target_value)
    if target.exists() and target.stat().st_size > 0 and config.verify_downloads:
        valid, _reason = validate_image_file(target, extension)
        if valid:
            return True
    elif target.exists() and target.stat().st_size > 0:
        return True

    attempt = download_url_to_path(url, target, extension, config, throttler)
    if attempt.status == "ok":
        return True

    record["error_type"] = attempt.error_type or "retry_failed"
    record["reason"] = attempt.reason or "整體補抓失敗"
    record["attempts"] = int(record.get("attempts", 0) or 0) + attempt.attempts
    return False


def perform_error_retries(
    config: DownloadConfig,
    error_logger: ErrorLogger,
    stats: RunStats,
    throttler: RequestThrottler,
) -> None:
    if config.all_retry <= 0:
        return

    for retry_round in range(1, config.all_retry + 1):
        error_logger.load()
        if not error_logger.records:
            error_logger.write()
            return

        retryable_records = [record for record in error_logger.records if bool(record.get("retryable"))]
        if not retryable_records:
            return

        stats.retry_rounds = retry_round
        print("")
        print(f"整體補抓第 {retry_round}/{config.all_retry} 輪，待補抓：{len(retryable_records)}")

        remaining: list[dict[str, Any]] = []
        changed = False
        retryable_ids = {str(record.get("id", "")) for record in retryable_records}

        for record in error_logger.records:
            if str(record.get("id", "")) not in retryable_ids:
                remaining.append(record)
                continue

            label = f"{record.get('series_name', '')}/{record.get('episode', '')} {record.get('target_name', '')}"
            print(f"補抓 {record.get('id')} {label}")
            if retry_error_record(config, record, throttler):
                print(f"補抓成功 {record.get('id')}")
                stats.downloaded += 1
                changed = True
                continue

            print(f"補抓失敗 {record.get('id')} - {record.get('error_type')}: {record.get('reason')}")
            remaining.append(record)

        error_logger.records = remaining
        error_logger.write()

        if not error_logger.records:
            return
        if not error_logger.has_retryable_records():
            return
        if not changed and retry_round >= config.all_retry:
            return


def run(config: DownloadConfig, dry_run: bool) -> int:
    if dry_run:
        return preview_plan(config)

    error_logger = ErrorLogger(ERROR_LOG_PATH)
    error_logger.reset()
    stats = RunStats()
    throttler = RequestThrottler(config.delay_seconds, config.request_jitter, config.adaptive_backoff)

    config.output_dir.mkdir(parents=True, exist_ok=True)
    print(f"輸出目錄：{config.output_dir}")
    print(f"圖片停止規則：每集連續 {config.consecutive_404_limit} 個 404。")
    print(f"集數自動停止規則：連續 {config.consecutive_missing_episode_limit} 個缺集。")
    print(f"非 404 錯誤規則：每集連續 {config.consecutive_error_limit} 個錯誤後跳過。")
    print(f"同集圖片併發：{config.image_workers}")
    if config.request_jitter is not None:
        print(f"請求隨機等待：{config.request_jitter[0]} 到 {config.request_jitter[1]} 秒。")
    else:
        print(f"請求固定等待：{config.delay_seconds} 秒。")
    if config.adaptive_extension_order:
        print("副檔名順序：同集自適應。")
    if config.adaptive_backoff:
        print("錯誤降速：啟用。")
    if config.all_retry > 0:
        print(f"整體補抓輪數：{config.all_retry}")
    if config.max_episode is None and config.probable_episodes:
        print("通用自動集數跳點：" + ", ".join(str(episode) for episode in config.probable_episodes))

    for series in build_series_list(config):
        print("")
        print(f"作品：{series.name}")
        print(f"網址前綴：{series.url_prefix.rstrip('/')}/")
        if series.max_episode is None and series.probable_episodes:
            print("本作品自動集數跳點：" + ", ".join(str(episode) for episode in series.probable_episodes))
        run_special_episodes(config, series, error_logger, stats, throttler)
        if series.max_episode is None:
            print(f"集數：{series.start_episode} 到自動偵測")
            run_auto_series(config, series, error_logger, stats, throttler)
        else:
            print(f"集數：{series.start_episode} 到 {series.max_episode}")
            run_fixed_series(config, series, error_logger, stats, throttler)

    perform_error_retries(config, error_logger, stats, throttler)
    error_logger.load()
    error_count = len(error_logger.error_records())
    notice_count = len(error_logger.notice_records())

    print("全部完成。")
    print(f"成功：{stats.downloaded}")
    print(f"略過：{stats.skipped}")
    print(f"找不到：{stats.not_found}")
    print(f"錯誤：{error_count}")
    if notice_count:
        print(f"提醒：{notice_count}")
    if config.all_retry > 0:
        print(f"整體補抓：{stats.retry_rounds}/{config.all_retry}")
    if error_logger.records:
        print(f"詳細錯誤/提醒：{ERROR_LOG_PATH}")
    return 0


def retry_errors_only(config: DownloadConfig) -> int:
    error_logger = ErrorLogger(ERROR_LOG_PATH)
    error_logger.load()
    if not error_logger.records:
        print(f"沒有找到可處理的錯誤檔：{ERROR_LOG_PATH}")
        return 0

    retryable_records = [record for record in error_logger.records if bool(record.get("retryable"))]
    notice_count = len(error_logger.notice_records())
    if not retryable_records:
        print("error.md 沒有可補抓的 retryable 錯誤。")
        if notice_count:
            print(f"保留提醒：{notice_count}")
        return 0

    stats = RunStats()
    throttler = RequestThrottler(config.delay_seconds, config.request_jitter, config.adaptive_backoff)
    print(f"只補抓 error.md：{ERROR_LOG_PATH}")
    print(f"可補抓錯誤：{len(retryable_records)}")
    if notice_count:
        print(f"保留提醒：{notice_count}")
    perform_error_retries(config, error_logger, stats, throttler)
    error_logger.load()

    print("補抓完成。")
    print(f"成功補抓：{stats.downloaded}")
    print(f"剩餘錯誤：{len(error_logger.error_records())}")
    if error_logger.notice_records():
        print(f"保留提醒：{len(error_logger.notice_records())}")
    if error_logger.records:
        print(f"詳細錯誤/提醒：{ERROR_LOG_PATH}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="批次下載多集編號圖片。",
        add_help=False,
        formatter_class=TraditionalChineseHelpFormatter,
    )
    parser._optionals.title = "選項"
    parser.add_argument(
        "-h",
        "--help",
        action="help",
        help="顯示此說明並結束。",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="config JSON 檔案路徑。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只列出範例網址與目標路徑，不下載。",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="只檢查既有集數資料夾，不下載。",
    )
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="只依照根目錄 error.md 補抓 retryable 錯誤，不重跑主下載流程。",
    )
    return parser.parse_args()


def main() -> int:
    configure_console_encoding()
    args = parse_args()
    if args.retry_errors:
        config = load_config(args.config, require_series_entries=False)
        return retry_errors_only(config)
    config = load_config(args.config)
    if args.check_only:
        return check_existing_downloads(config)
    return run(config, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
