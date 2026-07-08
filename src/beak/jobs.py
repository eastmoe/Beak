from __future__ import annotations

import hashlib
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .history import HistoryStore
from .schemas import Artifact, HistoryRecord, JobInfo, JobStatus, OutputType, RenderRequest, RenderResult
from .worker import WorkerError


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class JobRecord:
    job_id: str
    request: RenderRequest
    status: JobStatus
    created_at: str
    updated_at: str
    error: str | None = None
    result: RenderResult | None = None


class JobNotFound(KeyError):
    pass


class JobCancelled(RuntimeError):
    pass


class JobManager:
    def __init__(self, *, data_dir: Path, worker: object, max_workers: int = 4, history: HistoryStore | None = None) -> None:
        self.data_dir = data_dir
        self.jobs_dir = data_dir / "jobs"
        self.profiles_dir = data_dir / "profiles"
        self.history = history
        self.worker = worker
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="beak-job")
        self._jobs: dict[str, JobRecord] = {}
        self._futures: dict[str, Future[RenderResult]] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._lock = threading.RLock()
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.profiles_dir.mkdir(parents=True, exist_ok=True)

    def should_run_async(self, request: RenderRequest) -> bool:
        if request.async_mode is not None:
            return request.async_mode
        return request.output in {OutputType.COMPLETE_PAGE, OutputType.SINGLE_FILE}

    def create_job(self, request: RenderRequest) -> str:
        job_id = uuid.uuid4().hex
        now = utc_now()
        record = JobRecord(
            job_id=job_id,
            request=request,
            status=JobStatus.QUEUED,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._jobs[job_id] = record
            self._cancel_events[job_id] = threading.Event()
            self._record_history_locked(record)
            future = self.executor.submit(self._run_job, job_id)
            self._futures[job_id] = future
        return job_id

    def run_sync(self, request: RenderRequest) -> RenderResult:
        job_id = uuid.uuid4().hex
        now = utc_now()
        record = JobRecord(job_id, request, JobStatus.RUNNING, now, now)
        with self._lock:
            self._jobs[job_id] = record
            self._cancel_events[job_id] = threading.Event()
            self._record_history_locked(record)
        try:
            return self._execute(job_id, request)
        except Exception as exc:
            with self._lock:
                record.status = JobStatus.FAILED
                record.error = str(exc)
                record.updated_at = utc_now()
                self._record_history_locked(record)
            raise

    def get(self, job_id: str) -> JobInfo:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                raise JobNotFound(job_id)
            return self._info_from_record(record)

    def list_jobs(self, *, active_only: bool = False) -> list[JobInfo]:
        with self._lock:
            records = list(self._jobs.values())
            if active_only:
                records = [record for record in records if record.status in {JobStatus.QUEUED, JobStatus.RUNNING}]
            records.sort(key=lambda record: record.created_at, reverse=True)
            return [self._info_from_record(record) for record in records]

    def cancel(self, job_id: str) -> JobInfo:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                raise JobNotFound(job_id)
            if record.status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}:
                return self._info_from_record(record)
            cancel_event = self._cancel_events.setdefault(job_id, threading.Event())
            cancel_event.set()
            future = self._futures.get(job_id)
            cancelled_before_start = future.cancel() if future is not None else False
            record.status = JobStatus.CANCELLED
            record.error = "job cancelled" if cancelled_before_start else "job cancellation requested"
            record.updated_at = utc_now()
            self._record_history_locked(record)
            return self._info_from_record(record)

    def artifact_path(self, job_id: str, name: str) -> Path:
        info = self.get(job_id)
        artifacts = info.result.artifacts if info.result else []
        for artifact in artifacts:
            if artifact.name == name:
                path = Path(artifact.path)
                if path.exists() and path.is_file():
                    return path
        raise JobNotFound(f"{job_id}/{name}")

    def _run_job(self, job_id: str) -> RenderResult:
        with self._lock:
            record = self._jobs[job_id]
            cancel_event = self._cancel_events.setdefault(job_id, threading.Event())
            if cancel_event.is_set():
                record.status = JobStatus.CANCELLED
                record.error = "job cancelled"
                record.updated_at = utc_now()
                self._record_history_locked(record)
                raise JobCancelled("job cancelled")
            record.status = JobStatus.RUNNING
            record.updated_at = utc_now()
            self._record_history_locked(record)
        try:
            return self._execute(job_id, record.request)
        except JobCancelled as exc:
            with self._lock:
                record = self._jobs[job_id]
                record.status = JobStatus.CANCELLED
                record.error = str(exc)
                record.updated_at = utc_now()
                self._record_history_locked(record)
            raise
        except Exception as exc:  # noqa: BLE001 - the job status must capture all failures.
            with self._lock:
                record = self._jobs[job_id]
                cancel_event = self._cancel_events.setdefault(job_id, threading.Event())
                record.status = JobStatus.CANCELLED if cancel_event.is_set() else JobStatus.FAILED
                record.error = str(exc)
                record.updated_at = utc_now()
                self._record_history_locked(record)
            raise

    def _execute(self, job_id: str, request: RenderRequest) -> RenderResult:
        job_dir = self._job_dir(job_id, request)
        user_data_dir = self.profiles_dir / self._profile_key(job_id, request)
        cancel_event = self._cancel_events.setdefault(job_id, threading.Event())
        if cancel_event.is_set():
            raise JobCancelled("job cancelled")
        try:
            worker_result = self.worker.invoke(
                job_id=job_id,
                request=request,
                job_dir=job_dir,
                user_data_dir=user_data_dir,
                cancel_event=cancel_event,
            )
        except WorkerError:
            raise
        if cancel_event.is_set():
            raise JobCancelled("job cancelled")

        artifacts = [
            self._artifact_from_worker(job_id, artifact.name, artifact.content_type, Path(artifact.path))
            for artifact in worker_result.artifacts
        ]
        html = None
        if worker_result.html_path:
            html_path = Path(worker_result.html_path)
            if html_path.exists():
                html = html_path.read_text(encoding="utf-8", errors="replace")

        result = RenderResult(
            job_id=job_id,
            output=request.output,
            html=html,
            content_type="text/html; charset=utf-8" if html is not None else None,
            artifacts=artifacts,
            metadata=worker_result.metadata,
        )
        with self._lock:
            record = self._jobs[job_id]
            record.status = JobStatus.SUCCEEDED
            record.result = result
            record.error = None
            record.updated_at = utc_now()
            self._record_history_locked(record)
        return result

    def _job_dir(self, job_id: str, request: RenderRequest) -> Path:
        if request.output_dir is None:
            return self.jobs_dir / job_id
        output_dir = request.output_dir.expanduser()
        if not output_dir.is_absolute():
            output_dir = self.data_dir / output_dir
        return output_dir

    def _artifact_from_worker(self, job_id: str, name: str, content_type: str, path: Path) -> Artifact:
        size = path.stat().st_size if path.exists() else None
        return Artifact(
            name=name,
            content_type=content_type,
            path=str(path),
            download_url=f"/jobs/{job_id}/artifact/{name}",
            size_bytes=size,
        )

    @staticmethod
    def _info_from_record(record: JobRecord) -> JobInfo:
        return JobInfo(
            job_id=record.job_id,
            status=record.status,
            output=record.request.output,
            created_at=record.created_at,
            updated_at=record.updated_at,
            error=record.error,
            result=record.result,
        )

    def _record_history_locked(self, record: JobRecord) -> None:
        if self.history is None:
            return
        artifacts = record.result.artifacts if record.result else []
        metadata = record.result.metadata if record.result else {}
        self.history.upsert(
            HistoryRecord(
                job_id=record.job_id,
                status=record.status,
                output=record.request.output,
                url=str(record.request.url),
                engine=record.request.engine,
                created_at=record.created_at,
                updated_at=record.updated_at,
                error=record.error,
                artifacts=artifacts,
                metadata=metadata,
            )
        )

    @staticmethod
    def _profile_key(job_id: str, request: RenderRequest) -> str:
        prefix = request.engine
        if request.proxy is None:
            return f"{prefix}-{job_id}"
        digest = hashlib.sha256(request.proxy.model_dump_json().encode("utf-8")).hexdigest()[:16]
        return f"{prefix}-proxy-{digest}-{job_id}"
