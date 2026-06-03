"""
repryntt.routing.ai_queue — Thread-safe singleton AI request queue.

Ensures single-threaded access to the local LLM server (llama.cpp).
All processes submit requests here to prevent concurrent access chaos.

Extracted from: SAIGE/brain/brain_system.py lines 645-875
"""

import logging
import queue
import threading
import time
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)


class MasterAIQueue:
    """
    Master AI Request Queue — singleton, thread-safe, priority-aware.

    All repryntt processes submit AI requests here instead of calling the
    LLM server directly.  A single worker thread processes them sequentially
    because llama.cpp's /v1/chat/completions endpoint is single-threaded.
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialize()
        return cls._instance

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _initialize(self):
        self.request_queue = queue.Queue()
        self.worker_threads: List[threading.Thread] = []
        self.running = False
        self.active_requests: Dict[str, str] = {}
        self.processed_count = 0
        self.failed_count = 0
        self.max_parallel_workers = 1  # single-threaded llama server
        self.active_slots_lock = threading.Lock()
        logger.info("Master AI Queue initialized — 1 sequential worker")

    def start(self):
        """Start the queue worker(s)."""
        if self.running:
            return
        self.running = True
        for i in range(self.max_parallel_workers):
            t = threading.Thread(
                target=self._process_queue,
                daemon=True,
                name=f"AI-Queue-Worker-{i + 1}",
            )
            t.start()
            self.worker_threads.append(t)
        logger.warning(
            f"Master AI Queue started with {self.max_parallel_workers} worker"
        )

    def stop(self):
        """Stop all workers."""
        self.running = False
        for t in self.worker_threads:
            t.join(timeout=5)
        self.worker_threads.clear()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit_request(
        self,
        request_func: Callable,
        priority: int = 0,
        timeout: int = 300,
    ) -> Any:
        """
        Submit an AI request.  Blocks until the result is ready.

        Args:
            request_func: Zero-arg callable that makes the actual AI API call.
            priority: Higher = more important (0=normal, 1=high, 2=critical).
            timeout: Max processing time in seconds (queue wait is unbounded).
        """
        request_id = f"{int(time.time())}_{hash(str(request_func)) % 1000}"
        result_container: Dict[str, Any] = {
            "completed": False,
            "result": None,
            "error": None,
            "request_id": request_id,
            "processing_timeout": timeout,
            "started_processing": None,
        }

        queue_item = {
            "id": request_id,
            "func": request_func,
            "priority": priority,
            "timeout": timeout,
            "result_container": result_container,
            "submitted_at": time.time(),
        }

        with self._lock:
            self.request_queue.put(queue_item)
            qsize = self.request_queue.qsize()

        logger.info(
            f"AI REQUEST QUEUED: {request_id} (priority={priority}, queue={qsize})"
        )

        # Block until the worker sets completed=True.
        while not result_container["completed"]:
            time.sleep(0.1)

        if result_container["error"]:
            logger.error(f"AI REQUEST FAILED: {request_id}")
            raise result_container["error"]

        logger.debug(f"AI REQUEST COMPLETED: {request_id}")
        return result_container["result"]

    def submit_parallel_requests(
        self,
        request_funcs: List[Callable],
        priority: int = 0,
        timeout: int = 300,
    ) -> List[Any]:
        """Submit multiple requests (sequentially for now)."""
        results = []
        for func in request_funcs:
            try:
                results.append(self.submit_request(func, priority, timeout))
            except Exception as e:
                results.append(None)
                logger.error(f"Parallel request failed: {e}")
        return results

    def get_stats(self) -> Dict[str, Any]:
        with self.active_slots_lock:
            active = dict(self.active_requests)
        return {
            "queue_size": self.request_queue.qsize(),
            "active_requests": active,
            "active_workers": len(active),
            "max_workers": self.max_parallel_workers,
            "processed_count": self.processed_count,
            "failed_count": self.failed_count,
            "running": self.running,
        }

    # ------------------------------------------------------------------
    # Internal worker
    # ------------------------------------------------------------------

    def _process_queue(self):
        thread_name = threading.current_thread().name
        logger.info(f"{thread_name} running")

        while self.running:
            try:
                try:
                    queue_item = self.request_queue.get(timeout=0.5)
                except Exception:
                    continue

                request_id = queue_item["id"]
                request_func = queue_item["func"]
                result_container = queue_item["result_container"]

                with self.active_slots_lock:
                    self.active_requests[thread_name] = request_id

                result_container["started_processing"] = time.time()
                logger.info(f"{thread_name} PROCESSING: {request_id}")

                try:
                    start = time.time()
                    result = request_func()
                    elapsed = time.time() - start

                    result_container["result"] = result
                    result_container["completed"] = True
                    self.processed_count += 1
                    logger.info(
                        f"COMPLETED: {request_id} ({elapsed:.1f}s, "
                        f"total={self.processed_count})"
                    )
                except Exception as e:
                    logger.error(f"FAILED: {request_id} — {e}")
                    result_container["error"] = Exception(f"Request failed: {e}")
                    result_container["completed"] = True
                    self.failed_count += 1
                finally:
                    with self.active_slots_lock:
                        self.active_requests.pop(thread_name, None)

            except Exception as e:
                logger.error(f"Queue processing error: {e}")
                time.sleep(1)


# Module-level singleton — importable everywhere
master_ai_queue = MasterAIQueue()
