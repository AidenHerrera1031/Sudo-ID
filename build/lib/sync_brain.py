import hashlib
import json
import os
import re
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from dotenv import load_dotenv

from brain_common import DB_PATH, get_collection, probe_collection, reset_collection
from brain_settings import load_settings, should_ignore_dir, should_include_file

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

load_dotenv()

MAX_CHARS = 2200
OVERLAP_CHARS = 200
STATE_FILE = Path(DB_PATH) / "index_state.json"
MAX_SUMMARY_INPUT_CHARS = 12000
OPENAI_TIMEOUT_SECONDS = float(os.getenv("BRAIN_OPENAI_TIMEOUT", "8"))
CHAT_HISTORY_FILE = Path(
    os.getenv("BRAIN_CHAT_HISTORY_FILE", str(Path.home() / ".codex" / "history.jsonl"))
).expanduser()
CODEX_SESSIONS_DIR = Path(
    os.getenv("BRAIN_CODEX_SESSIONS_DIR", str(Path.home() / ".codex" / "sessions"))
).expanduser()
CHAT_SOURCE = os.getenv("BRAIN_CHAT_SOURCE", "auto").strip().lower()
CHAT_ENABLED = os.getenv("BRAIN_INDEX_CHAT_HISTORY", "1").strip().lower() not in {"0", "false", "no", "off"}
CHAT_MAX_ENTRIES = int(os.getenv("BRAIN_CHAT_MAX_ENTRIES", "1500"))
CHAT_MAX_SESSIONS = int(os.getenv("BRAIN_CHAT_MAX_SESSIONS", "50"))
CHAT_SUMMARY_WINDOW = int(os.getenv("BRAIN_CHAT_SUMMARY_WINDOW", "25"))
CHAT_UPSERT_BATCH_SIZE = int(os.getenv("BRAIN_CHAT_UPSERT_BATCH_SIZE", "20"))
CHAT_MAX_SESSION_FILES = int(os.getenv("BRAIN_CHAT_MAX_SESSION_FILES", "12"))
CHAT_MAX_SESSION_ENTRIES = int(os.getenv("BRAIN_CHAT_MAX_SESSION_ENTRIES", "400"))
try:
    CHAT_SUMMARY_CONCURRENCY = max(1, min(8, int(os.getenv("BRAIN_CHAT_SUMMARY_CONCURRENCY", "3"))))
except (TypeError, ValueError):
    CHAT_SUMMARY_CONCURRENCY = 3
SESSION_ID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
CHAT_SESSION_HASH_KEY_PREFIX = "__chat_session_hash__::"
PROJECT_IDENTITY_HASH_KEY = "__project_identity_hash__"

try:
    SYNC_THROTTLE_MS = max(0, int(os.getenv("BRAIN_SYNC_THROTTLE_MS", "0")))
except (TypeError, ValueError):
    SYNC_THROTTLE_MS = 0
SYNC_THROTTLE_SECONDS = SYNC_THROTTLE_MS / 1000.0
SYNC_PROGRESS_ENABLED = os.getenv("BRAIN_SYNC_PROGRESS", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
try:
    SYNC_PROGRESS_BAR_WIDTH = max(10, int(os.getenv("BRAIN_SYNC_PROGRESS_BAR_WIDTH", "28")))
except (TypeError, ValueError):
    SYNC_PROGRESS_BAR_WIDTH = 28
try:
    SYNC_HEARTBEAT_SECONDS = max(1.0, float(os.getenv("BRAIN_SYNC_HEARTBEAT_SECONDS", "2.0")))
except (TypeError, ValueError):
    SYNC_HEARTBEAT_SECONDS = 2.0
SYNC_IS_TTY = sys.stdout.isatty()


def iter_indexable_files(project_root: Path, settings):
    for root, dirs, files in os.walk(project_root):
        root_path = Path(root)
        dirs[:] = [
            d
            for d in dirs
            if not should_ignore_dir(root_path / d, project_root=project_root, settings=settings)
        ]
        if should_ignore_dir(root_path, project_root=project_root, settings=settings):
            continue
        for filename in files:
            file_path = root_path / filename
            if should_include_file(file_path, project_root=project_root, settings=settings):
                yield file_path


class SyncProgress:
    def __init__(self, total: int, enabled: bool):
        self.total = total
        self.enabled = enabled and total > 0
        self.is_tty = SYNC_IS_TTY
        self._last_rendered_at = 0.0
        self._last_width = 0

    def _bar(self, current: int) -> str:
        if self.total <= 0:
            return ""
        ratio = min(1.0, max(0.0, current / self.total))
        filled = int(round(SYNC_PROGRESS_BAR_WIDTH * ratio))
        return "[" + ("#" * filled) + ("-" * (SYNC_PROGRESS_BAR_WIDTH - filled)) + "]"

    def update(self, current: int, source: str):
        if not self.enabled:
            return

        now = time.time()
        is_last = current >= self.total
        if self.is_tty and not is_last and now - self._last_rendered_at < 0.08:
            return
        if not self.is_tty and not is_last and current % 25 != 0:
            return
        self._last_rendered_at = now

        percent = int((current / self.total) * 100)
        line = f"{self._bar(current)} {current}/{self.total} ({percent:3d}%) {source}"
        if self.is_tty:
            max_len = 160
            clipped = line[:max_len]
            padding = max(0, self._last_width - len(clipped))
            print(f"\r{clipped}{' ' * padding}", end="", flush=True)
            self._last_width = len(clipped)
            if is_last:
                print()
        else:
            print(f"Sync progress: {current}/{self.total} ({percent}%) {source}")


def emit_status(message: str) -> None:
    if SYNC_PROGRESS_ENABLED:
        print(f"[sync] {message}", flush=True)


def run_with_heartbeat(label: str, fn, *args):
    if not SYNC_PROGRESS_ENABLED:
        return fn(*args)

    stop_event = threading.Event()
    started_at = time.time()
    is_tty = SYNC_IS_TTY
    last_width = 0

    def render_transient(message: str) -> None:
        nonlocal last_width
        clipped = message[:160]
        padding = max(0, last_width - len(clipped))
        print(f"\r{clipped}{' ' * padding}", end="", flush=True)
        last_width = len(clipped)

    def clear_transient() -> None:
        nonlocal last_width
        if not is_tty or last_width == 0:
            return
        print(f"\r{' ' * last_width}\r", end="", flush=True)
        last_width = 0

    def heartbeat():
        while not stop_event.wait(SYNC_HEARTBEAT_SECONDS):
            elapsed = int(time.time() - started_at)
            message = f"[sync] {label} working... {elapsed}s"
            if is_tty:
                render_transient(message)
            else:
                emit_status(f"{label} working... {elapsed}s")

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        return fn(*args)
    finally:
        stop_event.set()
        thread.join(timeout=0.1)
        clear_transient()


def chunk_text(text: str, max_chars: int = MAX_CHARS, overlap: int = OVERLAP_CHARS):
    if len(text) <= max_chars:
        return [text]

    chunks = []
    step = max_chars - overlap
    start = 0
    while start < len(text):
        end = start + max_chars
        chunks.append(text[start:end])
        start += step
    return chunks


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_FILE)


def extract_symbols(text: str):
    pattern = re.compile(r"^\s*(?:def|class|function)\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)
    return pattern.findall(text)


def parse_chat_history(path: Path, max_entries: int):
    entries = []
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                session_id = str(record.get("session_id", "")).strip()
                try:
                    sort_ts = int(record.get("ts", 0) or 0)
                except (TypeError, ValueError):
                    sort_ts = 0
                # Keep timestamp metadata inside 32-bit bounds for broad backend compatibility.
                ts = max(-2147483648, min(2147483647, sort_ts))
                text = sanitize_chat_text(record.get("text", ""))
                if not text:
                    continue
                if not session_id:
                    session_id = "unknown"
                entries.append({"session_id": session_id, "ts": ts, "sort_ts": sort_ts, "text": f"user: {text}"})
    except OSError:
        return []

    if max_entries > 0 and len(entries) > max_entries:
        entries = entries[-max_entries:]
    return entries


def parse_iso_timestamp(value: str) -> int:
    value = str(value or "").strip()
    if not value:
        return 0
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return int(datetime.fromisoformat(value).timestamp())
    except Exception:
        return 0


def sanitize_chat_text(text: str, max_chars: int = 12000) -> str:
    text = str(text or "").replace("\x00", " ").strip()
    if len(text) > max_chars:
        text = text[:max_chars]
    cleaned = []
    for ch in text:
        code = ord(ch)
        if ch in {"\n", "\t"} or code >= 32:
            cleaned.append(ch)
    return "".join(cleaned).strip()


def parse_codex_sessions(path: Path, max_entries: int, max_session_files: int):
    entries = []
    if not path.exists():
        return entries

    files = []
    for session_file in path.rglob("*.jsonl"):
        try:
            mtime = session_file.stat().st_mtime
        except OSError:
            continue
        files.append((mtime, session_file))

    files.sort(key=lambda item: item[0], reverse=True)
    if max_session_files > 0:
        files = files[:max_session_files]

    for _, session_file in files:
        session_match = SESSION_ID_RE.search(session_file.name)
        session_id = session_match.group(1) if session_match else "unknown"
        try:
            with session_file.open("r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("type") != "response_item":
                        continue

                    payload = record.get("payload") or {}
                    if payload.get("type") != "message":
                        continue

                    role = str(payload.get("role", "")).strip().lower()
                    if role not in {"user", "assistant"}:
                        continue
                    if role == "assistant":
                        phase = str(payload.get("phase", "")).strip().lower()
                        if phase and phase != "final_answer":
                            continue

                    parts = payload.get("content") or []
                    snippets = []
                    for part in parts:
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") not in {"input_text", "output_text"}:
                            continue
                        text = str(part.get("text", "")).strip()
                        if text:
                            snippets.append(text)

                    message_text = sanitize_chat_text("\n".join(snippets))
                    if not message_text:
                        continue

                    sort_ts = parse_iso_timestamp(record.get("timestamp"))
                    if not sort_ts:
                        sort_ts = int(time.time())
                    ts = max(-2147483648, min(2147483647, sort_ts))
                    entries.append(
                        {
                            "session_id": session_id,
                            "ts": ts,
                            "sort_ts": sort_ts,
                            "text": f"{role}: {message_text}",
                        }
                    )
        except OSError:
            continue

    entries.sort(key=lambda item: item.get("sort_ts", 0))
    if max_entries > 0 and len(entries) > max_entries:
        entries = entries[-max_entries:]
    return entries


def load_chat_entries():
    source = CHAT_SOURCE
    if source not in {"auto", "history", "sessions"}:
        source = "auto"
    max_session_entries = max(1, min(CHAT_MAX_ENTRIES, CHAT_MAX_SESSION_ENTRIES))

    if source == "history":
        return "history", parse_chat_history(CHAT_HISTORY_FILE, CHAT_MAX_ENTRIES)
    if source == "sessions":
        return "sessions", parse_codex_sessions(CODEX_SESSIONS_DIR, max_session_entries, CHAT_MAX_SESSION_FILES)

    session_entries = parse_codex_sessions(CODEX_SESSIONS_DIR, max_session_entries, CHAT_MAX_SESSION_FILES)
    if session_entries:
        return "sessions", session_entries
    return "history", parse_chat_history(CHAT_HISTORY_FILE, CHAT_MAX_ENTRIES)


def hash_chat_entries(source: str, entries) -> str:
    parts = []
    for entry in entries:
        session_id = str(entry.get("session_id", ""))
        sort_ts = int(entry.get("sort_ts", entry.get("ts", 0)) or 0)
        text = str(entry.get("text", ""))
        parts.append(f"{session_id}|{sort_ts}|{text}")
    return digest_text(f"{source}\n" + "\n".join(parts))


def hash_chat_session_entries(session_entries) -> str:
    parts = []
    for entry in session_entries:
        sort_ts = int(entry.get("sort_ts", entry.get("ts", 0)) or 0)
        text = str(entry.get("text", ""))
        parts.append(f"{sort_ts}|{text}")
    return digest_text("\n".join(parts))


def chat_session_state_key(session_id: str) -> str:
    return f"{CHAT_SESSION_HASH_KEY_PREFIX}{session_id}"


def clear_chat_logs(collection):
    old = collection.get(where={"kind": "chat_log"}, include=[])
    if old.get("ids"):
        collection.delete(ids=old["ids"])


def clear_chat_records(collection):
    for kind in ("chat_log", "chat_summary"):
        old = collection.get(where={"kind": kind}, include=[])
        if old.get("ids"):
            collection.delete(ids=old["ids"])


def _reconstruct_text_from_chunks(existing_docs, existing_metas) -> str:
    chunks = []
    for doc_idx, meta in enumerate(existing_metas or []):
        meta = meta or {}
        if meta.get("kind") != "code_or_docs":
            continue
        try:
            chunk_index = int(meta.get("chunk_index", 0) or 0)
        except (TypeError, ValueError):
            chunk_index = 0
        doc = existing_docs[doc_idx] if doc_idx < len(existing_docs) else ""
        chunks.append((chunk_index, doc or ""))

    if not chunks:
        return ""

    chunks.sort(key=lambda item: item[0])
    rebuilt = chunks[0][1]
    for _, chunk in chunks[1:]:
        rebuilt += chunk[OVERLAP_CHARS:] if len(chunk) > OVERLAP_CHARS else chunk
    return rebuilt


def _find_change_anchor(lines: list[str], line_index: int) -> str:
    assignment_re = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=")
    symbol_re = re.compile(r"^(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")

    if not lines:
        return ""

    upper_bound = min(max(line_index, 0), len(lines) - 1)
    for idx in range(upper_bound, -1, -1):
        stripped = lines[idx].strip()
        if not stripped:
            continue
        assignment_match = assignment_re.match(stripped)
        if assignment_match:
            return assignment_match.group(1)
        symbol_match = symbol_re.match(stripped)
        if symbol_match:
            return symbol_match.group(1)
    return ""


def _extract_change_tokens(lines: list[str]) -> list[str]:
    tokens = []
    seen = set()
    for raw_line in lines:
        line = str(raw_line or "").strip()
        if not line or line in {"{", "}", "[", "]", "(", ")"}:
            continue
        for match in re.findall(r'["\']([^"\']{1,80})["\']', line):
            token = match.strip()
            if token and token not in seen:
                seen.add(token)
                tokens.append(token)
        cleaned = line.rstrip(",")
        if cleaned.startswith(".") and " " not in cleaned and cleaned not in seen:
            seen.add(cleaned)
            tokens.append(cleaned)
    return tokens


def summarize_notable_changes(previous_text: str, text: str) -> list[str]:
    if not previous_text.strip() or previous_text == text:
        return []

    previous_lines = previous_text.splitlines()
    current_lines = text.splitlines()
    matcher = SequenceMatcher(a=previous_lines, b=current_lines)
    changes = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        added_lines = current_lines[j1:j2]
        removed_lines = previous_lines[i1:i2]
        anchor = _find_change_anchor(current_lines, j1) or _find_change_anchor(previous_lines, i1)
        added_tokens = _extract_change_tokens(added_lines)
        removed_tokens = _extract_change_tokens(removed_lines)

        if added_tokens:
            token_text = ", ".join(f'"{token}"' for token in added_tokens[:3])
            changes.append(f"added {token_text}" + (f" in {anchor}" if anchor else ""))

        if removed_tokens:
            token_text = ", ".join(f'"{token}"' for token in removed_tokens[:3])
            changes.append(f"removed {token_text}" + (f" from {anchor}" if anchor else ""))

        if not added_tokens and not removed_tokens and anchor:
            changes.append(f"updated {anchor}")

        if len(changes) >= 3:
            break

    deduped = []
    seen = set()
    for change in changes:
        normalized = change.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(change)
    return deduped[:3]


def local_file_summary(source: str, text: str, changed: bool, previous_text: str = "") -> str:
    lines = text.splitlines()
    symbols = extract_symbols(text)[:8]
    notable_changes = summarize_notable_changes(previous_text, text) if changed else []
    if notable_changes:
        first_change = notable_changes[0]
        headline = first_change[:1].upper() + first_change[1:] + "."
    else:
        headline = "Updated file." if changed else "No new code changes."
    symbol_line = ", ".join(symbols) if symbols else "None detected"
    change_line = f"Notable Changes: {'; '.join(notable_changes)}.\n" if notable_changes else ""
    return (
        f"File: {source}\n"
        f"Summary: {headline}\n"
        f"Stats: {len(lines)} lines, {len(text)} chars.\n"
        f"{change_line}"
        f"Key Symbols: {symbol_line}\n"
        "Use this note for high-level context; pull code chunks only when needed."
    )


def local_chat_summary(session_id: str, session_entries) -> str:
    recent = session_entries[-min(len(session_entries), CHAT_SUMMARY_WINDOW) :]
    snippets = []
    for entry in recent[-5:]:
        snippets.append(" ".join(entry.get("text", "").split())[:220])

    bullets = "\n".join(f"- {s}" for s in snippets) if snippets else "- No chat lines found."
    return (
        f"Session: {session_id}\n"
        f"Summary: {len(session_entries)} captured messages indexed.\n"
        "Recent Highlights:\n"
        f"{bullets}\n"
        "Use this for context handoff; retrieve detailed chat chunks only when needed."
    )


def _read_text_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []


def build_project_identity_docs(project_root: Path) -> list[tuple[str, str, str]]:
    readme_lines = _read_text_lines(project_root / "README.md")
    pyproject_lines = _read_text_lines(project_root / "pyproject.toml")

    description = ""
    bullets = []
    commands = []

    for line in readme_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            description = stripped
            break

    in_what_it_does = False
    for line in readme_lines:
        stripped = line.strip()
        if stripped == "## What it does":
            in_what_it_does = True
            continue
        if in_what_it_does and stripped.startswith("## "):
            break
        if in_what_it_does and stripped.startswith("- "):
            bullets.append(stripped[2:].strip())
        if len(bullets) >= 4:
            break

    if not description:
        for line in pyproject_lines:
            stripped = line.strip()
            if stripped.startswith("description = "):
                description = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                break

    package_json = project_root / "package.json"
    try:
        package_data = json.loads(package_json.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        package_data = {}
    for name, cmd in (package_data.get("scripts") or {}).items():
        if name in {"setup", "start", "sync", "watch", "ask", "doctor", "tui"}:
            commands.append(f"{name}: {cmd}")

    overview_lines = []
    if description:
        overview_lines.append(f"Project Overview: {description}")
    if bullets:
        overview_lines.append("Core Capabilities:")
        overview_lines.extend(f"- {item}" for item in bullets[:4])
    if not overview_lines:
        overview_lines.append("Project Overview: Local project memory assistant.")

    commands_lines = ["Primary Commands:"]
    if commands:
        commands_lines.extend(f"- {line}" for line in commands[:6])
    else:
        commands_lines.extend(
            [
                "- brain start: guided onboarding",
                "- brain tui: full-screen terminal UI",
                "- brain sync: refresh project memory",
                "- brain ask: query indexed context",
                "- brain watch: auto-sync on file changes",
            ]
        )

    return [
        ("project::identity::overview", "project:overview", "\n".join(overview_lines).strip()),
        ("project::identity::commands", "project:commands", "\n".join(commands_lines).strip()),
    ]


def upsert_project_identity(collection, project_root: Path, current_state: dict, previous_state: dict) -> int:
    identity_docs = build_project_identity_docs(project_root)
    identity_hash = digest_text("\n\n".join(doc for _id, _source, doc in identity_docs))
    current_state[PROJECT_IDENTITY_HASH_KEY] = identity_hash
    if previous_state.get(PROJECT_IDENTITY_HASH_KEY) == identity_hash:
        return 0

    stale = collection.get(where={"kind": "project_identity"}, include=[])
    if stale.get("ids"):
        collection.delete(ids=stale["ids"])

    now = int(time.time())
    collection.upsert(
        ids=[doc_id for doc_id, _source, _doc in identity_docs],
        documents=[doc for _doc_id, _source, doc in identity_docs],
        metadatas=[
            {
                "source": source,
                "kind": "project_identity",
                "identity_type": source.split(":", 1)[1] if ":" in source else source,
                "indexed_at": now,
            }
            for _doc_id, source, _doc in identity_docs
        ],
    )
    return len(identity_docs)


def openai_file_summary(source: str, text: str, changed: bool, previous_summary: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or OpenAI is None:
        return ""

    openai_client = OpenAI(api_key=api_key, timeout=OPENAI_TIMEOUT_SECONDS)
    snippet = text[:MAX_SUMMARY_INPUT_CHARS]
    change_state = "changed" if changed else "unchanged"
    prompt = (
        f"File path: {source}\n"
        f"Change state since last sync: {change_state}\n"
        f"Previous summary:\n{previous_summary or 'None'}\n\n"
        "Current file content:\n"
        f"{snippet}\n\n"
        "Produce a compact technical summary for both humans and coding agents.\n"
        "Format exactly with headings:\n"
        "Summary:\n"
        "Important Details:\n"
        "Risks/Assumptions:\n"
        "Next Context To Track:\n"
        "Keep it concise and factual."
    )
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You summarize codebase changes for long-term project memory."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
    )
    return (response.choices[0].message.content or "").strip()


def openai_chat_summary(session_id: str, session_entries, previous_summary: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or OpenAI is None:
        return ""

    openai_client = OpenAI(api_key=api_key, timeout=OPENAI_TIMEOUT_SECONDS)
    recent = session_entries[-min(len(session_entries), CHAT_SUMMARY_WINDOW) :]
    joined = []
    for entry in recent:
        ts = entry.get("ts", 0)
        txt = entry.get("text", "")
        joined.append(f"[ts={ts}] {txt}")
    sample = "\n".join(joined)[:MAX_SUMMARY_INPUT_CHARS]
    prompt = (
        f"Session id: {session_id}\n"
        f"Previous summary:\n{previous_summary or 'None'}\n\n"
        "Recent chat content:\n"
        f"{sample}\n\n"
        "Summarize this session for project continuity.\n"
        "When the assistant gave a numbered list of recommendations, preserve that numbered list in Changes/Actions.\n"
        "Format exactly with headings:\n"
        "Summary:\n"
        "Decisions:\n"
        "Changes/Actions:\n"
        "Open Threads:\n"
        "Next Context To Track:\n"
        "Keep it concise and factual."
    )
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You distill engineering chat logs into durable project context."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
    )
    return (response.choices[0].message.content or "").strip()


def make_file_summary(
    source: str,
    text: str,
    changed: bool,
    previous_summary: str,
    previous_text: str = "",
) -> str:
    if not changed and previous_summary:
        return previous_summary
    try:
        summary = openai_file_summary(source, text, changed, previous_summary)
        if summary:
            return summary
    except Exception:
        pass
    return local_file_summary(source, text, changed, previous_text=previous_text)


def make_chat_summary(session_id: str, session_entries, previous_summary: str) -> str:
    try:
        summary = openai_chat_summary(session_id, session_entries, previous_summary)
        if summary:
            return summary
    except Exception:
        pass
    return local_chat_summary(session_id, session_entries)


def upsert_chat_summary(collection, session_id: str, summary_text: str) -> None:
    collection.upsert(
        ids=[f"chat:{session_id}::summary"],
        documents=[summary_text],
        metadatas=[
            {
                "source": f"chat:{session_id}",
                "kind": "chat_summary",
                "session_id": session_id,
                "indexed_at": int(time.time()),
            }
        ],
    )


def upsert_chat_summary_safe(collection, session_id: str, summary_text: str) -> None:
    for attempt in range(2):
        try:
            upsert_chat_summary(collection, session_id, summary_text)
            return
        except Exception as exc:
            if not is_recoverable_write_error(exc):
                raise
            if attempt == 1:
                raise
            time.sleep(0.1)


def load_chat_summaries(collection):
    result = collection.get(where={"kind": "chat_summary"}, include=["documents", "metadatas"])
    by_session = {}
    ids_by_session = {}
    documents = result.get("documents", []) or []
    metadatas = result.get("metadatas", []) or []
    ids = result.get("ids", []) or []
    for idx, meta in enumerate(metadatas):
        meta = meta or {}
        session_id = str(meta.get("session_id", "")).strip()
        if not session_id:
            continue
        doc = documents[idx] if idx < len(documents) else ""
        sid = ids[idx] if idx < len(ids) else ""
        by_session[session_id] = (doc or "").strip()
        if sid:
            ids_by_session[session_id] = sid
    return by_session, ids_by_session


def index_chat_history(collection, previous_state: dict, current_state: dict):
    if not CHAT_ENABLED:
        emit_status("Chat indexing disabled by BRAIN_INDEX_CHAT_HISTORY.")
        return 0, 0

    key = "__chat_history_hash__"
    chat_source, entries = load_chat_entries()
    emit_status(f"Chat stage: loaded {len(entries)} entries from {chat_source}.")
    if not entries:
        current_state.pop(key, None)
        if previous_state.get(key):
            clear_chat_records(collection)
            emit_status("Chat stage: cleared stale chat records (no entries found).")
        return 0, 0

    chat_hash = hash_chat_entries(chat_source, entries)
    current_state[key] = chat_hash

    by_session = defaultdict(list)
    for entry in entries:
        by_session[entry["session_id"]].append(entry)
    session_hashes = {}
    for session_id, session_entries in by_session.items():
        session_hash = hash_chat_session_entries(session_entries)
        session_hashes[session_id] = session_hash
        current_state[chat_session_state_key(session_id)] = session_hash

    if previous_state.get(key) == chat_hash:
        emit_status("Chat stage: no changes detected, skipping chat reindex.")
        return 0, 0

    emit_status("Chat stage: rebuilding chat records...")
    clear_chat_logs(collection)

    all_ids = []
    all_docs = []
    all_metas = []
    for entry_idx, entry in enumerate(entries):
        session_id = entry["session_id"]
        ts = entry["ts"]
        sort_ts = int(entry.get("sort_ts", ts) or ts)
        text = entry["text"]
        chunks = chunk_text(text, max_chars=1200, overlap=120)
        for chunk_idx, chunk in enumerate(chunks):
            key_text = f"{session_id}:{sort_ts}:{entry_idx}:{chunk_idx}:{chunk[:120]}"
            chunk_hash = hashlib.sha1(key_text.encode("utf-8", errors="ignore")).hexdigest()[:16]
            all_ids.append(f"chat::{session_id}::{sort_ts}::{entry_idx}::{chunk_idx}::{chunk_hash}")
            all_docs.append(chunk)
            all_metas.append(
                {
                    "source": f"chat:{session_id}",
                    "kind": "chat_log",
                    "session_id": session_id,
                    "ts": ts,
                    "indexed_at": int(time.time()),
                }
            )

    indexed_chat_chunks = 0
    total_chat_chunks = len(all_ids)
    if total_chat_chunks:
        emit_status(f"Chat stage: indexing {total_chat_chunks} chat chunks...")
    batch_size = max(1, CHAT_UPSERT_BATCH_SIZE)
    start = 0
    while start < len(all_ids):
        end = min(start + batch_size, len(all_ids))
        batch_ids = all_ids[start:end]
        batch_docs = all_docs[start:end]
        batch_metas = all_metas[start:end]
        try:
            collection.upsert(ids=batch_ids, documents=batch_docs, metadatas=batch_metas)
            indexed_chat_chunks += len(batch_ids)
            start = end
            if indexed_chat_chunks == total_chat_chunks or indexed_chat_chunks % 100 == 0:
                emit_status(f"Chat chunks: {indexed_chat_chunks}/{total_chat_chunks}")
        except Exception as exc:
            if not is_recoverable_write_error(exc):
                raise
            if len(batch_ids) <= 1:
                # Skip a single problematic chat chunk instead of failing the full sync.
                start = end
                continue
            batch_size = max(1, len(batch_ids) // 2)

    sorted_sessions = sorted(
        by_session.items(),
        key=lambda item: item[1][-1].get("sort_ts", item[1][-1].get("ts", 0)) if item[1] else 0,
        reverse=True,
    )
    if CHAT_MAX_SESSIONS > 0:
        sorted_sessions = sorted_sessions[:CHAT_MAX_SESSIONS]

    existing_summary_by_session, summary_ids_by_session = load_chat_summaries(collection)
    selected_session_ids = {session_id for session_id, _ in sorted_sessions}
    stale_summary_ids = [
        summary_ids_by_session[session_id]
        for session_id in summary_ids_by_session
        if session_id not in selected_session_ids
    ]
    if stale_summary_ids:
        collection.delete(ids=stale_summary_ids)

    sessions_to_generate = []
    reused_summaries = 0
    for session_id, session_entries in sorted_sessions:
        session_hash = session_hashes.get(session_id, "")
        previous_hash = previous_state.get(chat_session_state_key(session_id), "")
        existing_summary = existing_summary_by_session.get(session_id, "")
        if previous_hash == session_hash and existing_summary:
            reused_summaries += 1
            continue
        sessions_to_generate.append((session_id, session_entries, existing_summary))

    summary_count = 0
    total_summaries = len(sorted_sessions)
    total_to_generate = len(sessions_to_generate)
    if total_summaries:
        emit_status(
            f"Chat stage: generating {total_to_generate} of {total_summaries} session summaries "
            f"({reused_summaries} reused)..."
        )

    def summarize_sessions() -> int:
        completed = 0
        workers = min(CHAT_SUMMARY_CONCURRENCY, total_to_generate)
        if workers > 1:
            emit_status(f"Chat summary workers: {workers}")
            results = []
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_to_session = {
                    executor.submit(make_chat_summary, session_id, session_entries, previous_summary): (
                        session_id,
                        session_entries,
                    )
                    for session_id, session_entries, previous_summary in sessions_to_generate
                }
                for future in as_completed(future_to_session):
                    session_id, session_entries = future_to_session[future]
                    try:
                        summary_text = future.result()
                    except Exception:
                        summary_text = local_chat_summary(session_id, session_entries)
                    results.append((session_id, summary_text))

            for session_id, summary_text in results:
                upsert_chat_summary_safe(collection, session_id, summary_text)
                completed += 1
                if not SYNC_IS_TTY or completed == total_to_generate:
                    emit_status(f"Chat summaries refreshed: {completed}/{total_to_generate}")
            return completed

        for session_idx, (session_id, session_entries, previous_summary) in enumerate(
            sessions_to_generate, start=1
        ):
            summary_text = make_chat_summary(session_id, session_entries, previous_summary)
            upsert_chat_summary_safe(collection, session_id, summary_text)
            completed += 1
            if not SYNC_IS_TTY or session_idx == total_to_generate:
                emit_status(f"Chat summaries refreshed: {session_idx}/{total_to_generate}")
        return completed

    if total_to_generate:
        summary_count = run_with_heartbeat("Chat summaries", summarize_sessions)
    elif total_summaries:
        emit_status(f"Chat summaries refreshed: 0/{total_summaries} (all reused)")

    return indexed_chat_chunks, summary_count


def is_recoverable_write_error(exc: Exception) -> bool:
    message = str(exc).lower()
    if "failed to apply logs to the metadata segment" in message:
        return True
    if "error in compaction" in message:
        return True
    return False


def index_project(force_reindex: bool = False) -> None:
    collection = get_collection()
    project_root = Path(".").resolve()
    settings = load_settings(project_root)
    for config_error in settings.config_errors:
        emit_status(f"Config warning: {config_error}")
    previous_state = {} if force_reindex else load_state()
    current_state = {}
    file_paths = list(iter_indexable_files(project_root, settings=settings))

    indexed_files = 0
    indexed_chunks = 0
    summary_updates = 0
    identity_updates = 0
    chat_chunks = 0
    chat_summary_refreshes = 0
    emit_status(f"Stage 1/4: scanning complete, {len(file_paths)} candidate files.")
    if SYNC_THROTTLE_SECONDS > 0:
        emit_status(f"Throttle enabled: {SYNC_THROTTLE_MS}ms delay per file.")
    progress = SyncProgress(total=len(file_paths), enabled=SYNC_PROGRESS_ENABLED)

    for idx, file_path in enumerate(file_paths, start=1):
        source = str(file_path.relative_to(project_root))
        progress.update(idx, source)
        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        if not text.strip():
            continue

        file_hash = digest_text(text)
        current_state[source] = file_hash
        changed = previous_state.get(source) != file_hash

        existing = collection.get(where={"source": source}, include=["documents", "metadatas"])
        previous_summary = ""
        existing_docs = existing.get("documents", []) or []
        existing_metas = existing.get("metadatas", []) or []
        previous_text = _reconstruct_text_from_chunks(existing_docs, existing_metas)
        for doc_idx, meta in enumerate(existing_metas):
            meta = meta or {}
            if meta.get("kind") == "file_summary":
                doc = existing_docs[doc_idx] if doc_idx < len(existing_docs) else ""
                previous_summary = (doc or "").strip()
                break
        if existing.get("ids"):
            collection.delete(ids=existing["ids"])

        chunks = chunk_text(text)
        chunk_ids = [f"{source}::chunk_{i}" for i in range(len(chunks))]
        chunk_meta = [
            {
                "source": source,
                "chunk_index": i,
                "chunk_count": len(chunks),
                "indexed_at": int(time.time()),
                "kind": "code_or_docs",
                "file_hash": file_hash,
            }
            for i in range(len(chunks))
        ]
        collection.upsert(ids=chunk_ids, documents=chunks, metadatas=chunk_meta)

        summary_text = make_file_summary(
            source,
            text,
            changed,
            previous_summary,
            previous_text=previous_text,
        )
        summary_meta = {
            "source": source,
            "kind": "file_summary",
            "indexed_at": int(time.time()),
            "file_hash": file_hash,
        }
        collection.upsert(
            ids=[f"{source}::summary"],
            documents=[summary_text],
            metadatas=[summary_meta],
        )

        indexed_files += 1
        indexed_chunks += len(chunks)
        if changed:
            summary_updates += 1

        if SYNC_THROTTLE_SECONDS > 0:
            time.sleep(SYNC_THROTTLE_SECONDS)

    emit_status(f"Stage 2/4: file indexing complete ({indexed_files} files, {indexed_chunks} chunks).")
    emit_status("Stage 3/4: processing chat history and session summaries...")
    chat_chunks, chat_summary_refreshes = index_chat_history(collection, previous_state, current_state)
    emit_status(
        f"Stage 3/4: chat work complete ({chat_chunks} chunks, {chat_summary_refreshes} summary refreshes)."
    )

    emit_status("Stage 4/4: finalizing state and cleanup...")
    identity_updates = upsert_project_identity(collection, project_root, current_state, previous_state)
    if identity_updates:
        emit_status(f"Project identity summaries refreshed: {identity_updates}")
    removed_sources = sorted(
        source
        for source in (set(previous_state.keys()) - set(current_state.keys()))
        if not source.startswith("__")
    )
    for source in removed_sources:
        old = collection.get(where={"source": source}, include=[])
        if old.get("ids"):
            collection.delete(ids=old["ids"])

    save_state(current_state)
    print(
        f"Project memory updated: {indexed_files} files, {indexed_chunks} chunks, "
        f"{summary_updates} summary refreshes, {identity_updates} identity summaries, {chat_chunks} chat chunks, "
        f"{chat_summary_refreshes} chat summary refreshes, {len(removed_sources)} removals."
    )
    emit_status("Stage 4/4: done.")


def run_sync(force_reindex: bool = False) -> None:
    ok, detail = probe_collection()
    if not ok:
        print(
            f"Collection probe failed ({detail}). Rebuilding collection and retrying full sync...",
            file=sys.stderr,
        )
        reset_collection()
        save_state({})
        force_reindex = True
    try:
        index_project(force_reindex=force_reindex)
    except Exception as exc:
        if not is_recoverable_write_error(exc):
            raise
        print(
            "Chroma write issue detected. Rebuilding collection and retrying full sync...",
            file=sys.stderr,
        )
        reset_collection()
        save_state({})
        index_project(force_reindex=True)


if __name__ == "__main__":
    run_sync(force_reindex=False)
