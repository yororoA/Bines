"""Unified memory system.

The Memory Server process (``thingking/rag_server.py``) owns the SQLite source of truth
(``repository``) and the derived Chroma index (``vector_index``). Everything else talks to it
through ``client.MemoryClient``; ``channels`` normalizes local and NapCat input into
``models.ConversationEnvelope`` and ``scope_policy`` decides what each conversation may recall.
"""
