import argparse
import json
import os
import sys
from collections import OrderedDict
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from brain_common import get_collection

DEFAULT_RESULTS = 5
OPENAI_TIMEOUT_SECONDS = float(os.getenv("BRAIN_OPENAI_TIMEOUT", "8"))
SUMMARY_KINDS = {"file_summary", "chat_summary", "decision_log"}
SUMMARY_KIND_PRIORITY = {"chat_summary": 0, "decision_log": 1, "file_summary": 2}
CHAT_FIRST = os.getenv("BRAIN_CHAT_FIRST", "1").strip().lower() not in {"0", "false", "no", "off"}
DEFAULT_MODE = os.getenv("BRAIN_DEFAULT_MODE", "human").strip().lower()
if DEFAULT_MODE not in {"human", "codex", "both"}:
    DEFAULT_MODE = "human"
CHAT_HISTORY_FILE = Path(
    os.getenv("BRAIN_CHAT_HISTORY_FILE", str(Path.home() / ".codex" / "history.jsonl"))
).expanduser()

load_dotenv()


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


def current_session_source() -> str:
    try:
        lines = CHAT_HISTORY_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return ""

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        session_id = str(record.get("session_id", "")).strip()
        if session_id:
            return f"chat:{session_id}"
    return ""


def retrieve_context(query: str, n_results: int, summaries_only: bool = True):
    collection = get_collection()
    query_count = n_results if not summaries_only else max(n_results * 12, n_results)
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
    chat_log_candidates = []
    for idx, doc in enumerate(docs):
        meta = metas[idx] if idx < len(metas) else {}
        dist = dists[idx] if idx < len(dists) else None
        kind = str((meta or {}).get("kind", "")).strip()
        if kind in SUMMARY_KINDS:
            summary_candidates.append((doc, meta, dist))
            continue
        if kind == "chat_log":
            chat_log_candidates.append((doc, meta, dist))

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

    chat_log_candidates.sort(key=lambda item: item[2] if isinstance(item[2], (float, int)) else float("inf"))

    active_source = current_session_source()
    active_candidates = []
    if summaries_only and active_source.startswith("chat:"):
        active_session_id = active_source.split("chat:", 1)[1]
        where = {"$and": [{"kind": "chat_summary"}, {"session_id": active_session_id}]}
        active_result = collection.get(where=where, include=["documents", "metadatas"])
        active_docs = active_result.get("documents", []) or []
        active_metas = active_result.get("metadatas", []) or []
        for idx, doc in enumerate(active_docs):
            meta = active_metas[idx] if idx < len(active_metas) else {}
            active_candidates.append((doc, meta, 0.0))

    candidates = list(active_candidates) + list(summary_candidates)
    if len(candidates) < n_results:
        candidates.extend(chat_log_candidates)
    if active_source:
        candidates.sort(
            key=lambda item: 0 if str((item[1] or {}).get("source", "")).strip() == active_source else 1
        )

    out_docs, out_metas, out_dists = [], [], []
    seen_keys = set()
    for doc, meta, dist in candidates:
        kind = str((meta or {}).get("kind", "")).strip()
        source = str((meta or {}).get("source", "")).strip()
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


def summarize_with_openai(query: str, docs, metas, mode: str) -> str:
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
                    f"Retrieved context:\n{context_text}\n\n"
                    "If mode is `human`, return only this format:\n"
                    "Human Summary:\n"
                    "- Direct answer in 1 line\n"
                    "- Key points (max 4 bullets)\n"
                    "- Missing context (only if needed)\n"
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


def local_synthesis(query: str, docs, metas, mode: str) -> str:
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

    human_lines = ["Human Summary:"]
    if items:
        human_lines.append(f"- Built from {len(items)} matching memory entries for: {query}")
        for source, data in items[:max_points]:
            human_lines.append(f"- {source}: {data['snippet']}")
        if len(items) > max_points:
            human_lines.append(f"- +{len(items) - max_points} more entries (use --raw-only for full details)")
    else:
        human_lines.append("- No matching memory yet.")

    codex_lines = ["Codex Context:"]
    if items:
        for source, data in items[:max_points]:
            codex_lines.append(f"- [{source}] ({data['kind']}) {data['snippet']}")
        if len(items) > max_points:
            codex_lines.append(f"- Additional entries not shown: {len(items) - max_points}")
    else:
        codex_lines.append("- No matching memory yet.")

    if mode == "human":
        return "\n".join(human_lines)
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
    docs, metas, dists = retrieve_context(query, args.top_k, summaries_only=not args.include_code)

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
        summary = summarize_with_openai(query, docs, metas, args.mode)
        if summary:
            print(summary.strip())
            return
    except Exception as exc:
        print(f"OpenAI summarization skipped: {exc}", file=sys.stderr)

    print(local_synthesis(query, docs, metas, args.mode))


if __name__ == "__main__":
    main()
