"""Knowledge base: fetch → extract → chunk → embed → store, and retrieval at reply time.

Sync code (pymongo + httpx): ingestion runs in the RQ worker; API routes call into it via
a threadpool where needed.
"""
