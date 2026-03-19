import hashlib
import math
import os
import re
import subprocess
import sys

import chromadb
from chromadb.utils import embedding_functions

DB_PATH = os.getenv("BRAIN_DB_PATH", "./.codex_brain")
COLLECTION_BASE_NAME = os.getenv("BRAIN_COLLECTION_BASE_NAME", "project_context")
DEFAULT_ST_MODEL = "all-MiniLM-L6-v2"
DEFAULT_EMBED_PROVIDER = "local"
COLLECTION_PROBE_SKIP_ENV = "BRAIN_SKIP_COLLECTION_PROBE"


class LocalHashEmbeddingFunction:
    """Offline fallback embedding with stable hashed token vectors."""

    def __init__(self, dimension=512):
        self.dimension = dimension
        self._token_re = re.compile(r"[a-zA-Z0-9_]+")

    def _embed_text(self, text: str):
        vec = [0.0] * self.dimension
        tokens = self._token_re.findall((text or "").lower())
        if not tokens:
            return vec

        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            token_hash = int.from_bytes(digest, "big", signed=False)
            idx = token_hash % self.dimension
            sign = -1.0 if (token_hash & 1) else 1.0
            vec[idx] += sign

        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def __call__(self, input):
        return [self._embed_text(text) for text in input]

    def embed_documents(self, input):
        return self.__call__(input)

    def embed_query(self, input):
        if isinstance(input, str):
            return [self._embed_text(input)]
        return self.__call__(input)

    def name(self):
        return "local_hash_v1"


def create_embedding_function():
    provider = os.getenv("BRAIN_EMBED_PROVIDER", DEFAULT_EMBED_PROVIDER).lower().strip()

    if provider == "sentence-transformers":
        model = os.getenv("BRAIN_ST_MODEL", DEFAULT_ST_MODEL)
        try:
            return embedding_functions.SentenceTransformerEmbeddingFunction(model_name=model), "st"
        except Exception as exc:
            print(
                f"sentence-transformers embedding unavailable ({exc}); using local hash embeddings.",
                file=sys.stderr,
            )
            return LocalHashEmbeddingFunction(), "local"

    return LocalHashEmbeddingFunction(), "local"


def get_collection_config():
    embedding_fn, backend_key = create_embedding_function()
    collection_name = f"{COLLECTION_BASE_NAME}_{backend_key}"
    return embedding_fn, collection_name


def get_collection(get_or_create=True):
    db_client = chromadb.PersistentClient(path=DB_PATH)
    embedding_fn, collection_name = get_collection_config()
    if get_or_create:
        return db_client.get_or_create_collection(name=collection_name, embedding_function=embedding_fn)
    return db_client.get_collection(name=collection_name, embedding_function=embedding_fn)


def reset_collection():
    db_client = chromadb.PersistentClient(path=DB_PATH)
    embedding_fn, collection_name = get_collection_config()
    try:
        db_client.delete_collection(name=collection_name)
    except Exception:
        pass
    return db_client.get_or_create_collection(name=collection_name, embedding_function=embedding_fn)


def probe_collection(timeout_seconds: float = 20.0):
    if os.getenv(COLLECTION_PROBE_SKIP_ENV, "").strip():
        return True, "skipped"

    probe_code = (
        "from brain_common import get_collection\n"
        "col = get_collection()\n"
        "out = col.get(limit=1, include=['metadatas'])\n"
        "print(len(out.get('ids', [])))\n"
    )
    env = dict(os.environ)
    env[COLLECTION_PROBE_SKIP_ENV] = "1"
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe_code],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False, "timed out"

    if result.returncode == 0:
        return True, (result.stdout or "").strip()

    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()
    detail = stderr or stdout or f"probe failed with exit code {result.returncode}"
    return False, detail
