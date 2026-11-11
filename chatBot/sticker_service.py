"""Gateway-owned sticker collection; media never depends on a worker's filesystem."""
from chatBot.stickers import StickerLibrary
from chatBot.sticker_sources import resolve_sticker_source, resolve_favorite_source, validate_sticker_source


class StickerService:
    def __init__(self, service, library=None):
        self.service = service
        self.library = library or StickerLibrary(service.store)

    def call(self, identity, method, params, *, event_id=None, operation_id=None,
             sync_allowed=True, before_sync=None):
        if not isinstance(params, dict):
            raise ValueError("表情参数必须是对象")
        if method == "search":
            return self.library.search(identity, params.get("query", ""), params.get("limit", 5))
        if method == "resolve":
            return self.library.resolve(identity, params["sticker_id"])
        if method != "collect":
            raise ValueError("未知表情操作")
        if not operation_id:
            raise ValueError("收藏必须携带 operation_id")
        if type(params.get("shared", False)) is not bool:
            raise ValueError("shared 必须是布尔值")
        # Check access before looking up a previous operation's result.
        if params.get("shared") and not identity.privileged:
            raise PermissionError("公开表情只能由本机或主人私聊指定")
        if params.get("favorite_md5") and not identity.privileged:
            raise PermissionError("账号收藏只能由本机或主人私聊导入")
        request = {"scope": identity.scope, "conversation": identity.conversation,
                   "event_id": event_id, "params": params}
        new, previous = self.service.store.begin_operation(operation_id, "bines_collect_sticker", request)
        if not new:
            return self.reconcile(identity, operation_id, previous)
        try:
            if params.get("favorite_md5"):
                source = resolve_favorite_source(self.service, identity, params["favorite_md5"])
            else:
                source = resolve_sticker_source(
                    self.service, identity, event_id, params.get("message_id") or None, params.get("image_index", 0))
            metadata = self.library.save(
                identity, source["segment"], params.get("description", ""), params.get("tags", []),
                source_message_id=source.get("source_message_id") or "", shared=params.get("shared", False),
                expected_md5=(source.get("favorite") or {}).get("md5", ""),
                before_save=(lambda: validate_sticker_source(
                    self.service, identity, event_id, source["source_message_id"]))
                if source.get("source_message_id") else None)
            sticker_id = metadata["id"]
            if params.get("favorite_md5"):
                metadata = self.library.update_sync(identity, sticker_id, "confirmed")
            elif metadata.get("type") != "image":
                metadata = self.library.update_sync(identity, sticker_id, "not_applicable")
            elif identity.privileged and sync_allowed and metadata.get("qq_sync") != "confirmed":
                metadata = self._sync(identity, sticker_id, before_sync=before_sync)
            elif metadata.get("qq_sync") not in {"confirmed", "pending", "uncertain"}:
                metadata = self.library.update_sync(identity, sticker_id, "local_only")
            uncertain = metadata.get("qq_sync") in {"uncertain", "pending"}
            result = {"status": "uncertain" if uncertain else "ok", "local_saved": True,
                      "sticker": metadata, "operation_id": operation_id}
            if metadata.get("type") == "image":
                result["qq_operation_id"] = "sticker-favorite:" + metadata["md5"]
        except (PermissionError, ValueError, RuntimeError, OSError) as exc:
            # Source/download failures are definite local failures; no QQ write occurred.
            result = {"status": "failed", "local_saved": False, "msg": str(exc),
                      "operation_id": operation_id}
        self.service.store.complete_operation(operation_id, result)
        return result

    def reconcile(self, identity, operation_id, receipt=None):
        """Read QQ state to settle a prior collection receipt; never issue add again."""
        if receipt is None:
            with self.service.store.connection() as db:
                row = db.execute("SELECT action FROM operations WHERE id=?", (operation_id,)).fetchone()
            if not row or row["action"] != "bines_collect_sticker":
                return self.service.store.operation(operation_id)
            receipt = self.service.store.operation(operation_id)
        sticker = receipt.get("sticker") or {}
        if not sticker.get("id"):
            return receipt
        metadata = self.library.metadata(identity, sticker["id"])
        if receipt.get("status") != "uncertain" or not identity.privileged:
            return receipt
        try:
            confirmed = self._present(metadata["md5"])
        except Exception:
            confirmed = False
        if not confirmed:
            return receipt
        metadata = self.library.update_sync(identity, sticker["id"], "confirmed")
        result = {**receipt, "status": "ok", "sticker": metadata, "verified_by": "fetch_custom_face_detail"}
        self.service.store.complete_operation(operation_id, result)
        return result

    def _present(self, md5):
        result = self.service.client.raw_api("fetch_custom_face_detail", {"count": 100}, timeout=15)
        rows = result.get("data") if isinstance(result, dict) and result.get("status") == "ok" else []
        return isinstance(rows, list) and any(
            isinstance(row, dict) and str(row.get("md5", "")).lower() == md5.lower() for row in rows)

    def _sync(self, identity, sticker_id, *, before_sync=None):
        metadata = self.library.metadata(identity, sticker_id)
        # Reconcile before a write, including prior ambiguous acknowledgements.
        try:
            if self._present(metadata["md5"]):
                return self.library.update_sync(identity, sticker_id, "confirmed")
        except Exception:
            pass
        if metadata.get("qq_sync") in {"pending", "uncertain"}:
            return metadata  # No fresh write until the original receipt is resolved.
        segment = self.library.resolve(identity, sticker_id)
        if before_sync is not None and not before_sync():
            return metadata  # A new message/pause during download cancels the remote write.
        self.library.update_sync(identity, sticker_id, "pending")
        try:
            result = self.service.call(identity, "add_custom_face", {"file": segment["data"]["file"]},
                                       "sticker-favorite:" + metadata["md5"], timeout=30)
            if self._present(metadata["md5"]):
                return self.library.update_sync(identity, sticker_id, "confirmed")
            state = "failed" if result.get("status") == "failed" else "uncertain"
        except Exception:
            state = "uncertain"
        return self.library.update_sync(identity, sticker_id, state)
