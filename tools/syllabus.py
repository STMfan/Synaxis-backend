"""Offline Syllabus service shared by tools and authenticated API routes."""

import asyncio
import logging
import sqlite3

import numpy as np

from domain.errors import HarnessError
from domain.syllabus import SUBJECTS, SyllabusInfo, SyllabusResult
from storage.syllabus import MODEL, MODEL_FILES, SyllabusIndex, verify_imported_files

logger = logging.getLogger(__name__)


class LocalEmbedder:
    def __init__(self, directory, threads):
        from fastembed import TextEmbedding

        if not all((directory / name).is_file() for name in MODEL_FILES):
            raise FileNotFoundError("Local curriculum model missing")
        verify_imported_files(directory, MODEL_FILES, "models")
        self.engine = TextEmbedding(
            model_name=MODEL,
            cache_dir=str(directory),
            specific_model_path=str(directory),
            local_files_only=True,
            threads=threads,
            providers=["CPUExecutionProvider"],
        )

    def vector(self, query):
        tokenizer = self.engine.model.tokenizer
        tokenizer.no_truncation()
        try:
            if len(tokenizer.encode(query).ids) > 512:
                raise HarnessError("SYLLABUS_QUERY_TOO_LONG")
        finally:
            tokenizer.enable_truncation(max_length=512)
        vector = np.asarray(next(iter(self.engine.query_embed([query]))), dtype=np.float32)
        if vector.shape != (512,) or not np.isfinite(vector).all() or np.linalg.norm(vector) == 0:
            raise ValueError("Invalid curriculum query embedding")
        return vector


class SyllabusService:
    def __init__(self, settings, embedder_factory=LocalEmbedder):
        self.settings = settings
        self.directory = settings.syllabus_data_dir or settings.data_dir / "syllabus/data"
        self.model_directory = settings.syllabus_model_cache or settings.data_dir / "syllabus/models"
        self.embedder_factory = embedder_factory
        self.index = self.embedder = None
        self.reason = "SYLLABUS_NOT_INDEXED"
        # Includes model tokenization; no mutation or concurrent ONNX initialization races.
        self.gate = asyncio.Semaphore(1)

    def initialize(self):
        self.index = self.embedder = None
        self.reason = "SYLLABUS_NOT_INDEXED"
        if not self.settings.syllabus_enabled:
            self.reason = "SYLLABUS_DISABLED"
            return
        try:
            self.index = SyllabusIndex(self.directory)
        except FileNotFoundError:
            self.reason = "SYLLABUS_NOT_INDEXED"
            return
        except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
            self.reason = "SYLLABUS_INDEX_INVALID"
            logger.warning("Syllabus unavailable: %s (%s)", self.reason, type(exc).__name__)
            return
        self.reason = None
        if self.settings.syllabus_mode == "lexical":
            return
        try:
            embedder = self.embedder_factory(self.model_directory, self.settings.syllabus_threads)
            embedder.vector("课程标准")
            self.embedder = embedder
        except Exception as exc:
            self.reason = "SYLLABUS_MODEL_UNAVAILABLE"
            logger.warning("Syllabus unavailable: %s (%s)", self.reason, type(exc).__name__)

    def info(self):
        subjects = set(self.index.subjects) if self.index else set()
        return SyllabusInfo(
            availability="disabled"
            if not self.settings.syllabus_enabled
            else "unavailable"
            if self.reason
            else "ready",
            reason=self.reason,
            edition=self.index.manifest["edition"] if self.index else None,
            subjects=[key for key, name in SUBJECTS.items() if name in subjects],
            modes=(["lexical", "semantic", "hybrid"] if self.embedder else ["lexical"]) if self.index else [],
            default_mode=self.settings.syllabus_mode,
            model=MODEL if self.index else None,
            chunks=len(self.index.chunks) if self.index else 0,
            sections=len(self.index.sections) if self.index else 0,
        ).model_dump()

    async def search(self, request):
        mode = request.mode or self.settings.syllabus_mode
        result = dict(subject=request.subject, mode=mode, edition=self.info()["edition"])
        if (
            not self.settings.syllabus_enabled
            or self.index is None
            or (mode != "lexical" and self.embedder is None)
        ):
            return SyllabusResult(
                status="disabled" if not self.settings.syllabus_enabled else "unavailable",
                code=self.reason or "SYLLABUS_MODEL_UNAVAILABLE",
                **result,
                message="本地课标检索未就绪；没有检索证据，不能声称已查到课标。",
            ).model_dump()
        await self.gate.acquire()
        try:
            worker = asyncio.create_task(asyncio.to_thread(self._search, request, mode))
        except BaseException:
            self.gate.release()
            raise

        def finished(task):
            # The worker owns this gate, even after repeated caller cancellation.
            # Retrieve exceptions if its cancelled caller no longer awaits it.
            if not task.cancelled():
                task.exception()
            self.gate.release()

        worker.add_done_callback(finished)
        hits = await asyncio.shield(worker)
        return SyllabusResult(
            status="ok",
            count=len(hits),
            results=hits,
            **result,
            message="未找到匹配片段；不能据此认定知识点不在课程范围。" if not hits else None,
        ).model_dump()

    def _search(self, request, mode):
        try:
            vector = self.embedder.vector(request.query) if mode != "lexical" else None
            return self.index.search(
                request.query,
                mode=mode,
                vector=vector,
                top_k=request.top_k or self.settings.syllabus_top_k,
                subject=SUBJECTS.get(request.subject),
                category=request.course_category,
            )
        except HarnessError:
            raise
        except Exception as exc:
            raise HarnessError("SYLLABUS_RETRIEVAL_FAILED") from exc

    def section(self, identifier):
        if not self.settings.syllabus_enabled or self.index is None:
            raise HarnessError(self.reason or "SYLLABUS_NOT_INDEXED")
        return self.index.sections.get(identifier)
