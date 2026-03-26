import argparse
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from brain_settings import load_settings, should_ignore_dir, should_include_file

MEMORY_FILE = Path(os.getenv("BRAIN_DB_PATH", "./.codex_brain")) / "project_memory.json"
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "i",
    "in",
    "into",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "or",
    "our",
    "please",
    "should",
    "show",
    "that",
    "the",
    "this",
    "to",
    "update",
    "use",
    "we",
    "what",
    "where",
    "which",
    "with",
    "you",
}
SYMBOL_PATTERNS = [
    re.compile(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\("),
    re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)"),
    re.compile(r"^\s*(?:export\s+)?const\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s*)?\("),
    re.compile(r"^\s*(?:export\s+)?const\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s*)?[A-Za-z_$][A-Za-z0-9_$]*\s*=>"),
]
CRITICAL_FILES = {
    "brain_cli.py",
    "brain_common.py",
    "brain_doctor.py",
    "brain_init.py",
    "brain_settings.py",
    "brain_tui.py",
    "pyproject.toml",
    "requirements.txt",
    "sync_brain.py",
    "watch_brain.py",
}


@dataclass
class FileInfo:
    path: Path
    rel_path: str
    subsystem: str
    text: str
    path_tokens: set[str]
    text_tokens: set[str]
    symbols: list[tuple[str, int]]


def _project_root() -> Path:
    return Path(".").resolve()


def _safe_read_text(path: Path, max_chars: int = 50000) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    if len(data) > max_chars:
        return data[:max_chars]
    return data


def _tokenize(text: str) -> set[str]:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(text or ""))
    tokens = {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9_]+", expanded)
        if len(token) >= 2 and token.lower() not in STOPWORDS
    }
    return tokens


def _relative(path: Path, project_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(project_root.resolve())).replace("\\", "/")
    except Exception:
        return str(path).replace("\\", "/")


def _extract_symbols(text: str) -> list[tuple[str, int]]:
    symbols = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for pattern in SYMBOL_PATTERNS:
            match = pattern.search(line)
            if match:
                symbols.append((match.group(1), line_no))
                break
    return symbols


def _infer_subsystem(rel_path: str) -> str:
    lowered = rel_path.lower()
    name = Path(rel_path).name.lower()
    parts = [part for part in rel_path.split("/") if part]

    if name in {"readme.md", "commands.md", "handoff.md", "distribution.md"}:
        return "docs"
    if name in {"brain.toml", ".env", ".brainignore", "pyproject.toml", "requirements.txt", "package.json"}:
        return "config"
    if "watch" in name:
        return "watcher"
    if "sync" in name:
        return "sync"
    if "tui" in name:
        return "tui"
    if "cli" in name:
        return "cli"
    if "ask" in name:
        return "query"
    if "doctor" in name or "init" in name or "settings" in name:
        return "setup"
    if "memory" in name or "memorize" in name:
        return "memory"
    if "test" in lowered or "spec" in lowered or "smoke" in lowered:
        return "tests"
    if parts and parts[0] == "scripts":
        return "scripts"
    if parts and parts[0] not in {".", ""}:
        return parts[0]
    return Path(rel_path).stem.lower()


def _discover_files(project_root: Path) -> list[FileInfo]:
    settings = load_settings(project_root)
    items = []
    for root, dirs, files in os.walk(project_root):
        root_path = Path(root)
        dirs[:] = [
            name
            for name in dirs
            if not should_ignore_dir(root_path / name, project_root=project_root, settings=settings)
        ]
        for filename in files:
            path = root_path / filename
            if not should_include_file(path, project_root=project_root, settings=settings):
                continue
            rel_path = _relative(path, project_root)
            text = _safe_read_text(path)
            symbols = _extract_symbols(text)
            path_tokens = _tokenize(rel_path)
            text_tokens = set(path_tokens)
            symbol_tokens = set()
            for symbol, _line in symbols:
                symbol_tokens.update(_tokenize(symbol))
            text_tokens.update(symbol_tokens)
            text_tokens.update(_tokenize(text[:4000]))
            items.append(
                FileInfo(
                    path=path,
                    rel_path=rel_path,
                    subsystem=_infer_subsystem(rel_path),
                    text=text,
                    path_tokens=path_tokens,
                    text_tokens=text_tokens,
                    symbols=symbols,
                )
            )
    return items


def _git(project_root: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )


def _parse_git_status(project_root: Path) -> list[dict]:
    result = _git(project_root, ["status", "--short"])
    if result.returncode != 0:
        return []

    changes = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        code = line[:2]
        rest = line[3:].strip() if len(line) > 3 else ""
        path_text = rest.split(" -> ", 1)[-1].strip()
        if not path_text:
            continue
        changes.append({"status": code, "path": path_text})
    return changes


def _latest_commit_range(project_root: Path) -> tuple[str, str]:
    head = _git(project_root, ["rev-parse", "HEAD"])
    if head.returncode != 0:
        return "", ""
    parent = _git(project_root, ["rev-parse", "HEAD~1"])
    if parent.returncode != 0:
        return "", head.stdout.strip()
    return parent.stdout.strip(), head.stdout.strip()


def _files_for_commit_range(project_root: Path, start: str, end: str) -> list[str]:
    if not end:
        return []
    if not start:
        result = _git(project_root, ["show", "--pretty=", "--name-only", end])
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    args = ["diff", "--name-only", f"{start}..{end}"]
    result = _git(project_root, args)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _todays_commit_subjects(project_root: Path) -> list[str]:
    today_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00+00:00")
    result = _git(project_root, ["log", f"--since={today_start}", "--pretty=format:%s"])
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _todays_commit_paths(project_root: Path) -> list[str]:
    today_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00+00:00")
    result = _git(project_root, ["log", f"--since={today_start}", "--name-only", "--pretty=format:"])
    if result.returncode != 0:
        return []
    seen = []
    for line in result.stdout.splitlines():
        path = line.strip()
        if path and path not in seen:
            seen.append(path)
    return seen


def _load_project_summary(project_root: Path) -> str:
    readme_path = project_root / "README.md"
    for line in _safe_read_text(readme_path, max_chars=6000).splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return "Terminal-first project memory helper."


def _load_docs_text(project_root: Path) -> str:
    chunks = []
    for name in ("README.md", "COMMANDS.md", "HANDOFF.md", "DISTRIBUTION.md", ".env"):
        path = project_root / name
        if path.exists():
            chunks.append(_safe_read_text(path, max_chars=25000))
    return "\n".join(chunks)


def _load_persistent_memory() -> list[dict]:
    try:
        if MEMORY_FILE.exists():
            data = json.loads(MEMORY_FILE.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
    except Exception:
        return []
    return []


def _save_persistent_memory(entries: list[dict]) -> None:
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_FILE.write_text(json.dumps(entries, indent=2, sort_keys=True), encoding="utf-8")


def add_memory(kind: str, title: str, text: str) -> dict:
    from memorize import extract_and_store

    now = int(time.time())
    entry = {
        "id": f"memory-{now}",
        "kind": kind,
        "title": title,
        "text": text,
        "created_at": now,
    }
    entries = _load_persistent_memory()
    entries.append(entry)
    _save_persistent_memory(entries[-200:])

    note_lines = [
        f"Type: {kind}",
        f"Title: {title or 'Untitled'}",
        "",
        text.strip(),
    ]
    extract_and_store("\n".join(note_lines).strip())
    return entry


def _format_relative_time(timestamp: int) -> str:
    if not timestamp:
        return "unknown"
    delta = max(0, int(time.time()) - int(timestamp))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _candidate_docs_for_subsystems(subsystems: set[str]) -> list[str]:
    docs = set()
    if subsystems & {"cli", "tui", "setup", "query", "sync", "watcher"}:
        docs.update({"README.md", "COMMANDS.md"})
    if subsystems & {"config", "setup"}:
        docs.update({"README.md", "COMMANDS.md", "DISTRIBUTION.md"})
    if "watcher" in subsystems:
        docs.add("WATCHER_SMOKE_TEST.md")
    if "memory" in subsystems:
        docs.update({"README.md", "HANDOFF.md"})
    return sorted(docs)


def analyze_change_set(changed_paths: list[str], project_root: Optional[Path] = None) -> dict:
    root = project_root or _project_root()
    clean_paths = []
    for raw in changed_paths:
        text = str(raw or "").strip()
        if not text:
            continue
        if text.startswith("./"):
            text = text[2:]
        clean_paths.append(text)

    subsystems = sorted({_infer_subsystem(path) for path in clean_paths})
    changed_docs = {path for path in clean_paths if path.lower().endswith(".md")}
    doc_candidates = _candidate_docs_for_subsystems(set(subsystems))
    stale_docs = [path for path in doc_candidates if path not in changed_docs and (root / path).exists()]

    reviewer_questions = []
    if "config" in subsystems:
        reviewer_questions.append("Did config defaults and setup docs stay in sync with the code changes?")
    if "watcher" in subsystems or "sync" in subsystems:
        reviewer_questions.append("What is the expected reindex behavior after a file change, and was it verified?")
    if "cli" in subsystems or "tui" in subsystems:
        reviewer_questions.append("Is the beginner path still obvious, or did the command/UI surface get harder to use?")
    if stale_docs:
        reviewer_questions.append("Which docs should be updated before merge so the changed workflow is still discoverable?")
    if not reviewer_questions:
        reviewer_questions.append("What manual verification covers these changed files?")

    summary_parts = []
    if clean_paths:
        summary_parts.append(f"{len(clean_paths)} file{'s' if len(clean_paths) != 1 else ''}")
    if subsystems:
        summary_parts.append(", ".join(subsystems[:3]))
    summary = "Touched " + " | ".join(summary_parts) if summary_parts else "No file changes detected."

    return {
        "changed_files": clean_paths[:20],
        "subsystems": subsystems,
        "stale_docs": stale_docs[:6],
        "reviewer_questions": reviewer_questions[:4],
        "change_summary": summary,
    }


def _score_file(query: str, query_tokens: set[str], info: FileInfo) -> int:
    score = 0
    rel_lower = info.rel_path.lower()
    is_doc = rel_lower.endswith(".md")
    change_intent = bool(query_tokens & {"change", "edit", "modify", "update", "fix", "where"})
    if query and query in rel_lower:
        score += 24

    symbol_names = {name.lower() for name, _line in info.symbols}
    for token in query_tokens:
        if token in info.path_tokens:
            score += 8
        if token in info.text_tokens:
            score += 2
        if token in symbol_names:
            score += 6
        if token == info.subsystem:
            score += 6

    if change_intent:
        if is_doc:
            score -= 6
        else:
            score += 6

    if "watcher" in query_tokens and info.subsystem == "watcher":
        score += 6
    if "status" in query_tokens and ("status" in rel_lower or "status" in symbol_names):
        score += 6
    if "output" in query_tokens and ("output" in rel_lower or "output" in info.text_tokens):
        score += 3

    if any(name.lower() in query for name, _line in info.symbols):
        score += 6
    if is_doc and query_tokens & {"setup", "install", "command", "workflow", "handoff", "docs", "readme"}:
        score += 4
    elif is_doc:
        score -= 2
    else:
        score += 2
    return score


def _find_related_files(targets: list[FileInfo], index: list[FileInfo], query_tokens: set[str]) -> list[str]:
    if not targets:
        return []
    target_paths = {item.rel_path for item in targets}
    target_subsystems = {item.subsystem for item in targets}
    target_symbol_tokens = set()
    for item in targets:
        for symbol, _line in item.symbols[:8]:
            target_symbol_tokens.update(_tokenize(symbol))

    related = []
    for info in index:
        if info.rel_path in target_paths:
            continue
        score = 0
        if info.subsystem in target_subsystems:
            score += 4
        overlap = (target_symbol_tokens | query_tokens) & info.text_tokens
        score += min(5, len(overlap))
        if "test" in info.rel_path.lower() or "smoke" in info.rel_path.lower():
            score += 1
        if score > 0:
            related.append((score, info.rel_path))
    related.sort(key=lambda item: (-item[0], item[1]))
    return [path for _score, path in related[:6]]


def _suggest_tests(targets: list[FileInfo], index: list[FileInfo]) -> list[str]:
    suggestions = []
    target_tokens = set()
    for item in targets:
        target_tokens.update(item.path_tokens)
        target_tokens.update(_tokenize(item.subsystem))

    for info in index:
        lowered = info.rel_path.lower()
        if "test" not in lowered and "smoke" not in lowered:
            continue
        if target_tokens & info.path_tokens or info.subsystem in {item.subsystem for item in targets}:
            suggestions.append(info.rel_path)

    if not suggestions and any(item.subsystem == "watcher" for item in targets):
        suggestions.append("WATCHER_SMOKE_TEST.md")
    return suggestions[:5]


def _regression_questions(subsystems: set[str]) -> list[str]:
    questions = []
    if "watcher" in subsystems:
        questions.append("Watcher status, debounce, and background sync behavior could regress.")
    if "sync" in subsystems or "memory" in subsystems:
        questions.append("Index freshness or retrieval quality could change if sync assumptions moved.")
    if "cli" in subsystems or "tui" in subsystems:
        questions.append("New users may miss commands or flows if labels/help text became less obvious.")
    if "config" in subsystems or "setup" in subsystems:
        questions.append("Fresh installs may fail if starter files, env vars, or defaults drifted.")
    if not questions:
        questions.append("Any sibling files that rely on these names or workflows should be rechecked.")
    return questions[:4]


def print_guide() -> int:
    root = _project_root()
    index = _discover_files(root)
    memories = list(reversed(_load_persistent_memory()))[:4]
    overview = _load_project_summary(root)

    core = []
    preferred = [
        "brain_cli.py",
        "brain_tui.py",
        "sync_brain.py",
        "ask_brain.py",
        "watch_brain.py",
        "brain_settings.py",
    ]
    by_path = {item.rel_path: item for item in index}
    for name in preferred:
        if name in by_path:
            core.append((name, by_path[name].subsystem))

    print("Onboarding Guide")
    print(f"Project: {root}")
    print("")
    print("Overview:")
    print(f"- {overview}")
    print("")
    print("Core Files:")
    for path, subsystem in core:
        print(f"- {path} ({subsystem})")
    print("")
    print("Main Flows:")
    print("- setup: `brain start` or `brain tui` -> `brain init` -> `brain doctor` -> `brain sync`")
    print("- memory query: `brain ask` -> retrieval from `.codex_brain` -> local/OpenAI synthesis")
    print("- file changes: `brain watch` -> detect edits -> run sync -> update watcher status")
    print("")
    print("Environment And Config:")
    print("- `.env` stores `OPENAI_API_KEY` and optional Brain env vars.")
    print("- `.brainignore` controls what Brain skips during sync/watch.")
    print("- `brain.toml` controls index rules and watcher debounce.")
    print("")
    print("Known Traps:")
    print("- Missing `OPENAI_API_KEY` is allowed, but summaries fall back to weaker local heuristics.")
    print("- Watch mode can run in polling mode if `watchdog` is unavailable.")
    print("- Chroma issues usually require `brain doctor --fix` or `brain sync --force-reindex`.")
    if memories:
        print("")
        print("Durable Memory:")
        for item in memories:
            title = str(item.get("title", "")).strip() or "Untitled"
            kind = str(item.get("kind", "")).strip() or "note"
            print(f"- {kind}: {title} ({_format_relative_time(int(item.get('created_at', 0) or 0))})")
    return 0


def print_map(query: str, refactor: bool = False) -> int:
    root = _project_root()
    index = _discover_files(root)
    clean_query = " ".join((query or "").strip().lower().split())
    if not clean_query:
        print("Provide a task or question, for example: brain map \"watcher status\"")
        return 2

    query_tokens = _tokenize(clean_query)
    ranked = []
    for info in index:
        score = _score_file(clean_query, query_tokens, info)
        if score > 0:
            ranked.append((score, info))
    ranked.sort(key=lambda item: (-item[0], item[1].rel_path))

    if not ranked:
        print("No strong matches found.")
        return 1

    targets = [info for _score, info in ranked[:4]]
    related_files = _find_related_files(targets, index, query_tokens)
    subsystems = {info.subsystem for info in targets}

    title = "Safe Refactor Assist" if refactor else "Task To Code Map"
    print(title)
    print(f"Query: {query}")
    print("")
    print("Best Files:")
    for score, info in ranked[:4]:
        symbol_display = ", ".join(f"{name}:{line}" for name, line in info.symbols[:3]) or "no top-level symbols"
        print(f"- {info.rel_path} [{info.subsystem}] score={score} | symbols: {symbol_display}")
    print("")
    print("Likely Impact Areas:")
    for path in related_files[:5]:
        print(f"- {path}")
    if not related_files:
        print("- No extra impact files stood out from static scanning.")

    if refactor:
        print("")
        print("Tests To Run:")
        test_files = _suggest_tests(targets, index)
        if test_files:
            for path in test_files:
                print(f"- {path}")
        else:
            print("- No explicit tests matched. Plan a manual verification pass for the touched subsystem.")

        print("")
        print("Likely Regressions:")
        for line in _regression_questions(subsystems):
            print(f"- {line}")
    return 0


def _current_change_paths(project_root: Path) -> list[str]:
    status_changes = _parse_git_status(project_root)
    if status_changes:
        return [item["path"] for item in status_changes]

    today_paths = _todays_commit_paths(project_root)
    if today_paths:
        return today_paths

    start, end = _latest_commit_range(project_root)
    return _files_for_commit_range(project_root, start, end)


def _summary_change_source(project_root: Path) -> str:
    status_changes = _parse_git_status(project_root)
    if status_changes:
        return "worktree"
    subjects = _todays_commit_subjects(project_root)
    if subjects:
        return "today's commits"
    return "latest commit"


def _infer_goal_from_changes(subsystems: list[str], paths: list[str]) -> str:
    if not paths:
        return "No recent change goal could be inferred."
    if subsystems:
        focus = ", ".join(subsystems[:3])
        return f"Likely focus based on changed files: {focus}."
    return f"Likely focus based on changed files: {', '.join(paths[:3])}."


def print_summary(mode: str) -> int:
    root = _project_root()
    paths = _current_change_paths(root)
    analysis = analyze_change_set(paths, project_root=root)
    source = _summary_change_source(root)
    subjects = _todays_commit_subjects(root)[:3]

    if not paths:
        print("No worktree changes or recent commit diff detected.")
        return 0

    if mode == "handoff":
        print("Handoff")
        print(f"Scope: {source}")
        print("")
        print("Current Status:")
        print(f"- {analysis['change_summary']}")
        print(f"- {_infer_goal_from_changes(analysis['subsystems'], paths)}")
        print("")
        print("Changed Files:")
        for path in paths[:8]:
            print(f"- {path}")
        print("")
        print("Known Risks:")
        for question in analysis["reviewer_questions"]:
            print(f"- {question}")
        print("")
        print("Next 3 Tasks:")
        print("- Update stale docs if the changed workflow or config surface moved.")
        print("- Verify the highest-risk subsystem manually before handing off.")
        print("- Record any final decision with `brain decision --text ...`.")
        return 0

    if mode == "pr":
        print("PR Context")
        print(f"Source: {source}")
        print("")
        print("Summary:")
        print(f"- {analysis['change_summary']}")
        print(f"- {_infer_goal_from_changes(analysis['subsystems'], paths)}")
        if subjects:
            print(f"- Recent commit intent: {' | '.join(subjects)}")
        print("")
        print("Reviewer Focus:")
        for question in analysis["reviewer_questions"]:
            print(f"- {question}")
        print("")
        print("Potentially Stale Docs:")
        if analysis["stale_docs"]:
            for path in analysis["stale_docs"]:
                print(f"- {path}")
        else:
            print("- None detected.")
        return 0

    print("Today's Work Summary")
    print(f"Source: {source}")
    print("")
    print("What Changed:")
    print(f"- {analysis['change_summary']}")
    for path in paths[:6]:
        print(f"- {path}")
    print("")
    print("Why It Changed:")
    print(f"- {_infer_goal_from_changes(analysis['subsystems'], paths)}")
    if subjects:
        print(f"- Recent commit subjects: {' | '.join(subjects)}")
    print("")
    print("What Might Break:")
    for line in _regression_questions(set(analysis["subsystems"])):
        print(f"- {line}")
    return 0


def _discover_env_vars(paths: list[str], project_root: Path) -> list[str]:
    vars_found = set()
    pattern = re.compile(r"['\"]([A-Z][A-Z0-9_]{2,})['\"]")
    for rel_path in paths:
        path = project_root / rel_path
        text = _safe_read_text(path, max_chars=30000)
        for line in text.splitlines():
            if "getenv" not in line and "OPENAI_" not in line and "BRAIN_" not in line:
                continue
            for match in pattern.findall(line):
                if match.startswith(("BRAIN_", "OPENAI_")):
                    vars_found.add(match)
    return sorted(vars_found)


def print_release() -> int:
    root = _project_root()
    paths = _current_change_paths(root)
    analysis = analyze_change_set(paths, project_root=root)
    docs_text = _load_docs_text(root)
    env_vars = _discover_env_vars(paths, root)

    checks = []
    if paths:
        checks.append(("Worktree", "warn", f"{len(paths)} changed files need release review."))
    else:
        checks.append(("Worktree", "ok", "No local diff detected."))

    if analysis["stale_docs"]:
        checks.append(("Docs", "warn", "Likely stale docs: " + ", ".join(analysis["stale_docs"][:4])))
    else:
        checks.append(("Docs", "ok", "No obvious stale docs based on changed subsystems."))

    missing_env_docs = [name for name in env_vars if name not in docs_text]
    if missing_env_docs:
        checks.append(("Env Docs", "warn", "Undocumented env vars in changed code: " + ", ".join(missing_env_docs[:5])))
    else:
        checks.append(("Env Docs", "ok", "No missing env-doc issues detected from current changes."))

    risky = [path for path in paths if Path(path).name in CRITICAL_FILES]
    if risky:
        checks.append(("Risky Diff", "warn", "Critical paths changed: " + ", ".join(risky[:5])))
    else:
        checks.append(("Risky Diff", "ok", "No critical runtime or packaging file changed."))

    if any(path in {"pyproject.toml", "requirements.txt", "package.json"} for path in paths) and not (
        {"README.md", "DISTRIBUTION.md"} & set(paths)
    ):
        checks.append(("Packaging", "warn", "Packaging changed without install/distribution docs updates."))
    else:
        checks.append(("Packaging", "ok", "No packaging/doc mismatch detected."))

    print("Release Readiness")
    for name, status, detail in checks:
        print(f"[{status.upper()}] {name}: {detail}")

    print("")
    print("Reviewer Questions:")
    for question in analysis["reviewer_questions"]:
        print(f"- {question}")
    return 0


def decision_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="brain decision",
        description="Store or list durable project memories like conventions, decisions, and recurring bugs.",
    )
    parser.add_argument("--text", default="", help="Decision or memory text to store.")
    parser.add_argument("--title", default="", help="Short title for the memory.")
    parser.add_argument(
        "--kind",
        choices=["decision", "convention", "rejected", "bug", "rule"],
        default="decision",
        help="Memory category.",
    )
    parser.add_argument("--list", action="store_true", help="List recent durable memories.")
    args = parser.parse_args(argv)

    if args.list or not (args.text or "").strip():
        entries = list(reversed(_load_persistent_memory()))[:10]
        if not entries:
            print("No durable memories saved yet.")
            return
        print("Durable Memory")
        for item in entries:
            title = str(item.get("title", "")).strip() or "Untitled"
            print(
                f"- {item.get('kind', 'note')}: {title} | {_format_relative_time(int(item.get('created_at', 0) or 0))}"
            )
        return

    entry = add_memory(args.kind, args.title, args.text)
    print("")
    print("Durable memory saved.")
    print(f"- kind: {entry['kind']}")
    print(f"- title: {entry['title'] or 'Untitled'}")


def guide_main(argv=None):
    parser = argparse.ArgumentParser(prog="brain guide", description="Guided repo walkthrough for new contributors.")
    parser.parse_args(argv)
    raise SystemExit(print_guide())


def map_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="brain map",
        description="Map a task or question to likely files, symbols, and impact areas.",
    )
    parser.add_argument("query", nargs="+", help="Task or topic to map into code.")
    args = parser.parse_args(argv)
    raise SystemExit(print_map(" ".join(args.query), refactor=False))


def refactor_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="brain refactor",
        description="Show likely impact, regressions, and verification targets before a refactor.",
    )
    parser.add_argument("query", nargs="+", help="Refactor target or goal.")
    args = parser.parse_args(argv)
    raise SystemExit(print_map(" ".join(args.query), refactor=True))


def summarize_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="brain summarize",
        description="Summarize current work, prepare handoff notes, or generate PR context.",
    )
    parser.add_argument(
        "--mode",
        choices=["work", "handoff", "pr"],
        default="work",
        help="Summary shape to emit.",
    )
    args = parser.parse_args(argv)
    raise SystemExit(print_summary(args.mode))


def handoff_main(argv=None):
    parser = argparse.ArgumentParser(prog="brain handoff", description="Prepare a compact handoff summary.")
    parser.parse_args(argv)
    raise SystemExit(print_summary("handoff"))


def pr_main(argv=None):
    parser = argparse.ArgumentParser(prog="brain pr", description="Generate compact PR reviewer context.")
    parser.parse_args(argv)
    raise SystemExit(print_summary("pr"))


def release_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="brain release",
        description="Run lightweight pre-release checks for docs, env vars, and risky diffs.",
    )
    parser.parse_args(argv)
    raise SystemExit(print_release())
