import zmq


class RAGServerClient:
    """Simple ZMQ Client for RAG Server"""

    def __init__(self, zmq_context, host, port):
        self.ctx = zmq_context
        self.host = host
        self.port = port
        print(f"[RAGClient] Initialized: {host}:{port}", flush=True)

    def _req(self, method, **params):
        try:
            s = self.ctx.socket(zmq.REQ)
            s.setsockopt(zmq.RCVTIMEO, 30000)
            s.setsockopt(zmq.SNDTIMEO, 5000)
            s.setsockopt(zmq.LINGER, 0)

            s.connect(f"tcp://{self.host}:{self.port}")
            s.send_json({"method": method, "params": params})
            res = s.recv_json()
            s.close()
            return res
        except zmq.error.Again:
            print(f"[RAGClient] Timeout calling {method}", flush=True)
            try:
                s.close()
            except Exception:
                pass
            return None
        except Exception as e:
            print(f"[RAGClient] Request failed ({method}): {e}", flush=True)
            try:
                s.close()
            except Exception:
                pass
            return None

    def add_to_summary_buffer(self, content, meta=None, importance_score=7):
        return self._req("add_to_summary_buffer", content=content, meta=meta, importance_score=importance_score)

    def add_qq_log(self, content, meta=None):
        return self._req("add_qq_log", content=content, meta=meta)
