import argparse
import os
from collections import OrderedDict
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from brain_common import get_collection

DEFAULT_RESULTS = 5
OPENAI_TIMEOUT_SECONDS = float(os.getenv("BRAIN_OPENAI_TIMEOUT", "8"))
SUMMARY_KINDS = {"project_identity", "file_summary", "chat_summary", "decision_log"}
SUMMARY_KIND_PRIORITY = {"project_identity": 0, "decision_log": 1, "file_summary": 2, "chat_summary": 3}
CHAT_FIRST = os.getenv("BRAIN_CHAT_FIRST", "0").strip().lower() not in {"0", "false", "no", "off"}
DEFAULT_MODE = os.getenv("BRAIN_DEFAULT_MODE", "human").strip().lower()
if DEFAULT_MODE not in {"human", "codex", "both"}:
    DEFAULT_MODE = "human"
DEFAULT_SCOPE = os.getenv("BRAIN_DEFAULT_SCOPE", "mixed").strip().lower()
if DEFAULT_SCOPE not in {"mixed", "project", "chat"}:
    DEFAULT_SCOPE = "mixed"
DEFAULT_RENDER = os.getenv("BRAIN_DEFAULT_RENDER", "plain").strip().lower()
if DEFAULT_RENDER not in {"plain", "sections"}:
    DEFAULT_RENDER = "plain"

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent


def compact_snippet(text: str, limit: int = 150) -> str:
    cleaned = " ".join((text or "").strip().split())
    for marker in ("Recent Highlights:", "Use this for context handoff;", "Use this note for high-level context;"):
        if marker in cleaned:
            cleaned = cleaned.split(marker, 1)[0].strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 3)].rstrip() + "..."


def compact_chat_summary_snippet(text: str, limit: int = 220) -> str:
    raw = str(text or "")
    if "Recent Highlights:" in raw:
        tail = raw.split("Recent Highlights:", 1)[1]
        bullets = []
        for line in tail.splitlines():
            line = line.strip()
            if not line.startswith("-"):
                continue
            bullets.append(" ".join(line[1:].strip().split()))
        if bullets:
            merged = " | ".join(bullets[-2:])
            if len(merged) <= limit:
                return merged
            return merged[: max(0, limit - 3)].rstrip() + "..."
    return compact_snippet(raw, limit=limit)


def confidence_label(metas) -> str:
    kinds = {str((meta or {}).get("kind", "")).strip() for meta in metas}
    if "project_identity" in kinds:
        return "high"
    if "file_summary" in kinds or "decision_log" in kinds:
        return "medium"
    if "chat_summary" in kinds or "chat_log" in kinds:
        return "low"
    return "low"


def format_human_sections(answer: str, key_points, files, missing_context: str, confidence: str) -> str:
    lines = ["Answer:"]
    lines.append(f"- {answer}")
    lines.append("Key Points:")
    if key_points:
        lines.extend(f"- {point}" for point in key_points[:3])
    else:
        lines.append("- No additional key points.")
    lines.append("Files:")
    if files:
        lines.extend(f"- {path}" for path in files[:3])
    else:
        lines.append("- No project files were strong matches.")
    lines.append("Missing Context:")
    lines.append(f"- {missing_context or 'None.'}")
    lines.append("Confidence:")
    lines.append(f"- {confidence}")
    return "\n".join(lines)


def looks_like_project_overview_query(query: str) -> bool:
    normalized = " ".join((query or "").strip().lower().split())
    if not normalized:
        return False
    phrases = (
        "what is this project about",
        "what is this repo about",
        "what is this repository about",
        "what does this project do",
        "what does this repo do",
        "project overview",
        "repo overview",
        "repository overview",
    )
    return any(phrase in normalized for phrase in phrases)


def project_overview_fallback() -> str:
    readme_path = PROJECT_ROOT / "README.md"
    description = ""
    bullets = []

    try:
        lines = readme_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        description = stripped
        break

    in_what_it_does = False
    for line in lines:
        stripped = line.strip()
        if stripped == "## What it does":
            in_what_it_does = True
            continue
        if in_what_it_does and stripped.startswith("## "):
            break
        if in_what_it_does and stripped.startswith("- "):
            bullets.append(stripped[2:].strip())
        if len(bullets) >= 2:
            break

    if not description:
        pyproject_path = PROJECT_ROOT / "pyproject.toml"
        try:
            for line in pyproject_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                stripped = line.strip()
                if stripped.startswith("description = "):
                    description = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        except OSError:
            pass

    lines_out = []
    if description:
        lines_out.append(description)
    lines_out.extend(bullets[:2])
    return "\n".join(lines_out).strip()


def retrieve_context(query: str, n_results: int, summaries_only: bool = True, scope: str = "mixed"):
    collection = get_collection()
    query_count = n_results if not summaries_only else max(n_results * 40, 80)
    results = collection.query(
        query_texts=[query],
        n_results=query_count,
        include=["documents", "metadatas", "distances"],
    )
    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    dists = results.get("distances", [[]])[0]

    if not summaries_only:
        return docs[:n_results], metas[:n_results], dists[:n_results]

    summary_candidates = []
    project_summary_candidates = []
    chat_summary_candidates = []
    chat_log_candidates = []
    project_other_candidates = []
    for idx, doc in enumerate(docs):
        meta = metas[idx] if idx < len(metas) else {}
        dist = dists[idx] if idx < len(dists) else None
        kind = str((meta or {}).get("kind", "")).strip()
        if kind in SUMMARY_KINDS:
            summary_candidates.append((doc, meta, dist))
            if kind == "chat_summary":
                chat_summary_candidates.append((doc, meta, dist))
            else:
                project_summary_candidates.append((doc, meta, dist))
            continue
        if kind == "chat_log":
            chat_log_candidates.append((doc, meta, dist))
            continue
        project_other_candidates.append((doc, meta, dist))

    if CHAT_FIRST:
        def sort_key(item):
            _, meta, dist = item
            kind = str((meta or {}).get("kind", "")).strip()
            priority = SUMMARY_KIND_PRIORITY.get(kind, 99)
            recency = 0
            if kind == "chat_summary":
                try:
                    recency = -int((meta or {}).get("indexed_at", 0) or 0)
                except (TypeError, ValueError):
                    recency = 0
            distance = dist if isinstance(dist, (float, int)) else float("inf")
            return (priority, recency, distance)

        summary_candidates.sort(key=sort_key)
    else:
        summary_candidates.sort(key=lambda item: item[2] if isinstance(item[2], (float, int)) else float("inf"))
        project_summary_candidates.sort(key=lambda item: item[2] if isinstance(item[2], (float, int)) else float("inf"))
        chat_summary_candidates.sort(key=lambda item: item[2] if isinstance(item[2], (float, int)) else float("inf"))

    chat_log_candidates.sort(key=lambda item: item[2] if isinstance(item[2], (float, int)) else float("inf"))
    project_other_candidates.sort(key=lambda item: item[2] if isinstance(item[2], (float, int)) else float("inf"))

    if scope == "project":
        candidates = list(project_summary_candidates)
        if len(candidates) < n_results:
            candidates.extend(project_other_candidates)
    elif scope == "chat":
        candidates = list(chat_summary_candidates)
        if len(candidates) < n_results:
            candidates.extend(chat_log_candidates)
    else:
        candidates = list(project_summary_candidates) + list(chat_summary_candidates)
        if len(candidates) < n_results:
            candidates.extend(project_other_candidates)
        if len(candidates) < n_results:
            candidates.extend(chat_log_candidates)

    out_docs, out_metas, out_dists = [], [], []
    seen_keys = set()
    for doc, meta, dist in candidates:
        kind = str((meta or {}).get("kind", "")).strip()
        source = str((meta or {}).get("source", "")).strip()
        if scope == "project" and kind in {"chat_summary", "chat_log"}:
            continue
        if scope == "chat" and kind not in {"chat_summary", "chat_log"}:
            continue
        if kind == "chat_log":
            key = (
                source,
                str((meta or {}).get("ts", "")),
                str((meta or {}).get("chunk_index", "")),
            )
        else:
            key = (source, kind)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        out_docs.append(doc)
        out_metas.append(meta)
        out_dists.append(dist)
        if len(out_docs) >= n_results:
            break
    return out_docs, out_metas, out_dists


def summarize_with_openai(query: str, docs, metas, mode: str, render: str = "plain", scope: str = "mixed") -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return ""

    openai_client = OpenAI(api_key=api_key, timeout=OPENAI_TIMEOUT_SECONDS)
    context_blocks = []
    for idx, doc in enumerate(docs):
        meta = metas[idx] if idx < len(metas) else {}
        src = meta.get("source", "unknown")
        kind = meta.get("kind", "unknown")
        context_blocks.append(f"[source={src} kind={kind}]\n{doc}")

    context_text = "\n\n---\n\n".join(context_blocks)

    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a project's long-term memory assistant. "
                    "Answer only from retrieved context. If context is insufficient, say what is missing. "
                    "Keep output clean and easy to scan. "
                    "Use short bullets and plain language for humans."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Question: {query}\n"
                    f"Output mode: {mode}\n\n"
                    f"Render style: {render}\n"
                    f"Retrieved context:\n{context_text}\n\n"
                    f"Retrieval scope: {scope}\n"
                    "If mode is `human` and render is `plain`, return only the answer text.\n"
                    "No heading, no provenance, no preamble, max 5 short lines.\n"
                    "If mode is `human` and render is `sections`, use exactly these headings:\n"
                    "Answer:\n"
                    "Key Points:\n"
                    "Files:\n"
                    "Missing Context:\n"
                    "Confidence:\n"
                    "Keep each section concise.\n"
                    "If mode is `codex`, return only this format:\n"
                    "Codex Context:\n"
                    "- Facts/decisions (max 6 bullets)\n"
                    "If mode is `both`, return both sections:\n"
                    "Human Summary:\nCodex Context:\n"
                    "Keep total output under 180 words."
                ),
            },
        ],
        temperature=0.2,
    )
    return response.choices[0].message.content or ""


def local_synthesis(query: str, docs, metas, mode: str, render: str = "plain", scope: str = "mixed") -> str:
    grouped = OrderedDict()
    for idx, doc in enumerate(docs):
        meta = metas[idx] if idx < len(metas) else {}
        source = meta.get("source", "unknown")
        kind = meta.get("kind", "unknown")
        if source in grouped:
            continue
        snippet = compact_snippet(doc)
        if kind == "chat_summary":
            snippet = compact_chat_summary_snippet(doc)
        grouped[source] = {
            "kind": kind,
            "snippet": snippet,
        }

    max_points = 4
    items = list(grouped.items())
    has_non_chat_context = any(
        str(data.get("kind", "")).strip() not in {"chat_summary", "chat_log"} for _source, data in items
    )
    has_project_identity = any(str(data.get("kind", "")).strip() == "project_identity" for _source, data in items)

    if mode == "human" and looks_like_project_overview_query(query) and not has_project_identity:
        fallback = project_overview_fallback()
        if fallback:
            if render == "sections":
                return format_human_sections(
                    answer=fallback.splitlines()[0].strip(),
                    key_points=[line.strip() for line in fallback.splitlines()[1:3] if line.strip()],
                    files=["README.md", "pyproject.toml"],
                    missing_context="Detailed architecture summaries are not indexed yet.",
                    confidence="medium",
                )
            return fallback

    human_snippets = []
    seen_human_snippets = set()
    for _source, data in items:
        snippet = str(data.get("snippet", "")).strip()
        if not snippet:
            continue
        normalized = snippet.lower()
        if normalized in seen_human_snippets:
            continue
        seen_human_snippets.add(normalized)
        human_snippets.append(snippet)

    human_lines = ["Human Summary:"]
    if human_snippets:
        human_lines.extend(f"- {snippet}" for snippet in human_snippets[:2])
    else:
        human_lines.append("- No matching memory yet.")

    file_sources = []
    seen_sources = set()
    for source, data in items:
        kind = str(data.get("kind", "")).strip()
        if kind in {"chat_summary", "chat_log"}:
            continue
        if source in seen_sources:
            continue
        seen_sources.add(source)
        file_sources.append(source)

    missing_context = ""
    if not items:
        missing_context = "No matching memory yet."
    elif scope == "project" and not file_sources:
        missing_context = "No strong project-memory matches. Run `brain sync` to refresh project summaries."
    elif not has_non_chat_context:
        missing_context = "Results are chat-heavy. Use `--scope project` after a fresh sync for repo-focused answers."

    codex_lines = ["Codex Context:"]
    if items:
        for source, data in items[:max_points]:
            codex_lines.append(f"- [{source}] ({data['kind']}) {data['snippet']}")
        if len(items) > max_points:
            codex_lines.append(f"- Additional entries not shown: {len(items) - max_points}")
    else:
        codex_lines.append("- No matching memory yet.")

    if mode == "human":
        if render == "sections":
            if human_snippets:
                return format_human_sections(
                    answer=human_snippets[0],
                    key_points=human_snippets[1:3],
                    files=file_sources,
                    missing_context=missing_context or "None.",
                    confidence=confidence_label(metas),
                )
            return format_human_sections(
                answer="No matching memory yet.",
                key_points=[],
                files=[],
                missing_context="Run `brain sync` to build project memory.",
                confidence="low",
            )
        if human_snippets:
            return "\n".join(human_snippets[:2])
        return "No matching memory yet."
    if mode == "codex":
        return "\n".join(codex_lines)
    return "\n".join(human_lines + [""] + codex_lines)


def format_raw_context(query: str, docs, metas, dists) -> str:
    lines = [f"Query: {query}", ""]
    for idx, doc in enumerate(docs):
        meta = metas[idx] if idx < len(metas) else {}
        dist = dists[idx] if idx < len(dists) else None
        src = meta.get("source", "unknown")
        kind = meta.get("kind", "unknown")
        score = f"{dist:.4f}" if isinstance(dist, (float, int)) else "n/a"
        lines.append(f"[{idx + 1}] source={src} kind={kind} distance={score}")
        lines.append(doc.strip())
        lines.append("")
    return "\n".join(lines).strip()


def main():
    parser = argparse.ArgumentParser(description="Query project memory from .codex_brain")
    parser.add_argument("query", nargs="+", help="Question to ask the memory store")
    parser.add_argument("-k", "--top-k", type=int, default=DEFAULT_RESULTS, help="Number of chunks to retrieve")
    parser.add_argument(
        "--mode",
        choices=["human", "codex", "both"],
        default=DEFAULT_MODE,
        help="Response format for people, coding agents, or both",
    )
    parser.add_argument(
        "--scope",
        choices=["mixed", "project", "chat"],
        default=DEFAULT_SCOPE,
        help="Bias retrieval toward project summaries, chat memory, or a mix.",
    )
    parser.add_argument(
        "--render",
        choices=["plain", "sections"],
        default=DEFAULT_RENDER,
        help="Plain text or a compact sectioned answer renderer for human mode.",
    )
    parser.add_argument(
        "--include-code",
        action="store_true",
        help="Search all entries, including raw code chunks (default searches summaries only)",
    )
    parser.add_argument(
        "--raw-only",
        action="store_true",
        help="Skip synthesis and print retrieved chunks directly",
    )
    args = parser.parse_args()

    query = " ".join(args.query).strip()
    docs, metas, dists = retrieve_context(
        query,
        args.top_k,
        summaries_only=not args.include_code,
        scope=args.scope,
    )

    if not docs:
        if args.include_code:
            print("No context found. Run: python3 sync_brain.py")
        else:
            print("No summary context found. Run: python3 sync_brain.py (or use --include-code).")
        return

    if args.raw_only:
        print(format_raw_context(query, docs, metas, dists))
        return

    try:
        summary = summarize_with_openai(query, docs, metas, args.mode, render=args.render, scope=args.scope)
        if summary:
            print(summary.strip())
            return
    except Exception:
        pass

    print(local_synthesis(query, docs, metas, args.mode, render=args.render, scope=args.scope))


if __name__ == "__main__":
    main()
