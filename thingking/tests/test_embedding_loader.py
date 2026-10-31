"""Embedding startup uses the mounted cache before touching the network."""
import unittest
from unittest.mock import Mock, patch

import helpers  # noqa: F401 - installs project and thingking/src paths
from memory import embedding


class EmbeddingLoaderTests(unittest.TestCase):
    def setUp(self):
        embedding._cached = None
        self.addCleanup(setattr, embedding, "_cached", None)

    def test_complete_cache_is_loaded_offline_first(self):
        loaded = object()
        with patch("huggingface_hub.snapshot_download", return_value="/cache/model") as resolve, \
                patch("langchain_huggingface.HuggingFaceEmbeddings", return_value=loaded) as constructor, \
                patch("memory.embedding._detect_device", return_value="cpu"):
            result = embedding.load_embedder()
            self.assertIs(result.delegate, loaded)
            self.assertIs(embedding.load_embedder(), result)
            self.assertTrue(result.bines_identity["normalize_embeddings"])
        self.assertTrue(resolve.call_args.kwargs["local_files_only"])
        self.assertEqual(constructor.call_args.kwargs["model_name"], "/cache/model")
        self.assertEqual(constructor.call_count, 1)

    def test_incomplete_cache_falls_back_to_download_once(self):
        loaded = object()
        constructor = Mock(return_value=loaded)
        with patch("huggingface_hub.snapshot_download", side_effect=[OSError("missing cache"), "/cache/downloaded"]) as resolve, \
                patch("langchain_huggingface.HuggingFaceEmbeddings", constructor), \
                patch("memory.embedding._detect_device", return_value="cpu"), \
                patch.dict("os.environ", {"BINES_EMBEDDING_OFFLINE": ""}):
            self.assertIs(embedding.load_embedder().delegate, loaded)
        self.assertTrue(resolve.call_args_list[0].kwargs["local_files_only"])
        self.assertNotIn("local_files_only", resolve.call_args_list[1].kwargs)
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(constructor.call_args.kwargs["model_name"], "/cache/downloaded")
        self.assertEqual(constructor.call_count, 1)

    def test_revision_is_resolved_and_included_in_identity(self):
        with patch("huggingface_hub.snapshot_download", return_value="/cache/models/snapshots/commit-b") as resolve, \
                patch("langchain_huggingface.HuggingFaceEmbeddings", return_value=object()), \
                patch("memory.embedding._detect_device", return_value="cpu"), \
                patch("memory.embedding._revision", return_value="commit-b"), \
                patch.dict("os.environ", {"RAG_EMBEDDING_REVISION": "version-b"}):
            result = embedding.load_embedder()
        self.assertEqual(resolve.call_args.kwargs["revision"], "version-b")
        self.assertEqual(result.bines_identity["model_revision"], "commit-b")
        self.assertEqual(result.bines_identity["requested_revision"], "version-b")

    def test_explicit_offline_mode_never_downloads(self):
        constructor = Mock()
        with patch("huggingface_hub.snapshot_download", side_effect=OSError("missing cache")), \
                patch("langchain_huggingface.HuggingFaceEmbeddings", constructor), \
                patch("memory.embedding._detect_device", return_value="cpu"), \
                patch.dict("os.environ", {"BINES_EMBEDDING_OFFLINE": "true"}):
            with self.assertRaises(OSError):
                embedding.load_embedder()
        self.assertEqual(constructor.call_count, 0)


if __name__ == "__main__":
    unittest.main()
