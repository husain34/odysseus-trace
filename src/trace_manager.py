"""
trace_manager.py

Bridges TRACE (Hierarchical B+Tree memory) with Odysseus Agent Mode.
Handles async offloading of blocking LLM calls and per-session lock management.
"""

import os
import time
import json
import asyncio
from pathlib import Path
from functools import partial
import logging
from typing import Dict, List, Optional

# Import vendored TRACE
from src.trace_memory.ctree import CTree
from src.trace_memory.vector_db import VectorDatabase
from src.trace_memory.prompt_synthesizer import PromptSynthesizer
from src.embeddings import get_embedding_client

logger = logging.getLogger(__name__)

def _get_embed_fn():
    """Bridge Odysseus's embedding client to TRACE's embed_fn."""
    client = get_embedding_client()
    if client:
        def embed(text: str) -> list:
            # client.encode returns np.ndarray, we need a standard list for TRACE
            result = client.encode([text], normalize_embeddings=True)
            if len(result) > 0:
                return result[0].tolist()
            return []
        return embed
    return None

class TRACESession:
    """Holds the tree, vector DB, synthesizer, and lock for a single agent session."""
    def __init__(self, tree: CTree, vdb: VectorDatabase, synth: PromptSynthesizer):
        self.tree = tree
        self.vdb = vdb
        self.synth = synth
        # B.1 FIX: Lock created inside __init__ so it always binds to the
        # running event loop, not at import/class-definition time.
        self._lock = asyncio.Lock()

    async def add_exchange(
        self,
        user_msg: str,
        assistant_msg: str,
        tool_summary: str = None,
    ):
        """Safely appends to the tree via thread pool to avoid blocking the event loop.

        Parameters
        ----------
        user_msg      : The raw user message for this turn.
        assistant_msg : The full LLM response for this turn.
        tool_summary  : Optional one-line summary of tools used this turn (F.3).
                        Stored as a system message so TRACE can recall tool usage.
        """
        async with self._lock:
            loop = asyncio.get_event_loop()
            messages = [
                {"role": "user",      "content": user_msg},
                {"role": "assistant", "content": assistant_msg},
            ]
            # F.3: Prepend tool-usage context as a system message so the tree
            # captures what tools were invoked during this exchange.
            if tool_summary:
                messages.insert(0, {"role": "system", "content": tool_summary})
            await loop.run_in_executor(None, partial(self.tree.add, messages))

    async def get_system_prompt(self, user_query: str) -> str:
        """Generates the multi-path RAG context block."""
        embed_fn = _get_embed_fn()
        query_vector = embed_fn(user_query) if embed_fn else []

        loop = asyncio.get_event_loop()
        # Prompt synthesis uses vector search, so we offload it just in case it blocks
        prompt = await loop.run_in_executor(
            None,
            partial(
                self.synth.synthesize_prompt,
                user_query=user_query,
                query_vector=query_vector,
                active_node=self.tree.current_node,
                recent_messages=self.tree.conversation[-4:],  # Inject last 2 exchanges
            )
        )
        return prompt

class TRACEManager:
    """Global registry for active TRACE sessions."""
    _instances: Dict[tuple, TRACESession] = {}
    # B.1 FIX: Do NOT create asyncio.Lock() at class-definition time.
    # asyncio.Lock() must be created inside a running event loop, otherwise
    # it silently binds to a different loop and raises RuntimeError on any
    # server restart or uvicorn --reload cycle.
    _registry_lock: Optional[asyncio.Lock] = None

    @classmethod
    def _get_registry_lock(cls) -> asyncio.Lock:
        """Lazily create the registry lock inside the running event loop."""
        if cls._registry_lock is None:
            cls._registry_lock = asyncio.Lock()
        return cls._registry_lock

    @classmethod
    async def get_or_create(cls, owner: str, session_id: str, model: str) -> TRACESession:
        key = (owner, session_id)
        # Fast-path: check without acquiring the lock first.
        if key in cls._instances:
            return cls._instances[key]
        async with cls._get_registry_lock():
            # Re-check inside the lock in case another coroutine raced us.
            if key not in cls._instances:
                cls._instances[key] = await cls._load_or_new(owner, session_id, model)
        return cls._instances[key]

    @classmethod
    async def _load_or_new(cls, owner: str, session_id: str, model: str) -> TRACESession:
        save_dir = Path(f"data/trace/{owner}")
        save_dir.mkdir(parents=True, exist_ok=True)
        
        json_path = save_dir / f"{session_id}.json"
        db_path = save_dir / f"{session_id}.db"

        # Initialize VDB
        vdb = VectorDatabase(str(db_path))

        # Load or create CTree
        if json_path.exists():
            try:
                tree = CTree.load(str(json_path), model=model)
                logger.info(f"Loaded existing TRACE tree for {session_id}")
            except Exception as e:
                logger.error(f"Failed to load TRACE tree: {e}. Starting fresh.")
                tree = CTree(model=model, auto_save_path=str(json_path))
        else:
            tree = CTree(model=model, auto_save_path=str(json_path))

        # Inject dependencies
        tree.vdb = vdb
        tree.embed_fn = _get_embed_fn()

        synth = PromptSynthesizer(ctree=tree, vector_db=vdb)
        return TRACESession(tree, vdb, synth)
