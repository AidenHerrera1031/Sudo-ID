import os
import sys
import time
from uuid import uuid4

from dotenv import load_dotenv
from openai import OpenAI

from brain_common import get_collection

load_dotenv()
OPENAI_TIMEOUT_SECONDS = float(os.getenv("BRAIN_OPENAI_TIMEOUT", "8"))


def distill_memory(raw_text: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return raw_text.strip()

    try:
        client = OpenAI(api_key=api_key, timeout=OPENAI_TIMEOUT_SECONDS)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Create a concise technical memory note from the session text. "
                        "Return plain text with these headings:\n"
                        "Summary:\n"
                        "Decisions:\n"
                        "Changes:\n"
                        "Open Questions:\n"
                        "Next Steps:"
                    ),
                },
                {"role": "user", "content": raw_text},
            ],
            temperature=0.2,
        )
        return (response.choices[0].message.content or "").strip()
    except Exception:
        return raw_text.strip()


def extract_and_store(raw_text: str) -> None:
    collection = get_collection()
    distilled_memory = distill_memory(raw_text)
    if not distilled_memory:
        print("Nothing to store.")
        return

    note_id = f"memory_{int(time.time())}_{uuid4().hex[:8]}"
    metadata = {
        "source": "memory_note",
        "kind": "decision_log",
        "timestamp": int(time.time()),
    }
    collection.add(ids=[note_id], documents=[distilled_memory], metadatas=[metadata])
    print(f"Saved memory note: {note_id}")
    print("")
    print(distilled_memory)


if __name__ == "__main__":
    user_input = sys.stdin.read()
    if user_input.strip():
        extract_and_store(user_input)
    else:
        print("Usage: remember < paste text >")
