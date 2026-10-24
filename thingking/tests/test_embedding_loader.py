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
            self.assertIs(embedding.load_embedder(), loaded)
        self.assertTrue(resolve.call_args.kwargs["local_files_only"])
        self.assertEqual(constructor.call_args.kwargs["model_name"], "/cache/model")
        self.assertEqual(constructor.call_count, 1)

    def test_incomplete_cache_falls_back_to_download_once(self):
        loaded = object()
        constructor = Mock(return_value=loaded)
        with patch("huggingface_hub.snapshot_download", side_effect=OSError("missing cache")) as resolve, \
                patch("langchain_huggingface.HuggingFaceEmbeddings", constructor), \
                patch("memory.embedding._detect_device", return_value="cpu"), \
                patch.dict("os.environ", {"BINES_EMBEDDING_OFFLINE": ""}):
            self.assertIs(embedding.load_embedder(), loaded)
        self.assertTrue(resolve.call_args.kwargs["local_files_only"])
        self.assertEqual(constructor.call_args.kwargs["model_name"],
                         resolve.call_args.kwargs["repo_id"])
        self.assertEqual(constructor.call_count, 1)

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
