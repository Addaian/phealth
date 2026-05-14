"""
Ingest API router -- accepts a SimplePractice Data Export and runs the
ingestion pipeline in the background.

``POST /ingest/simplepractice-zip`` takes a multipart upload of the export ZIP,
returns ``202 Accepted`` immediately, and processes the export off the request
path (PRD §5.3). The endpoint is guarded by the ``X-API-Key`` header
(app.core.security); the read endpoints are intentionally open for the local
demo (PRD §5.4).

The orchestrator (app.ingest.simplepractice_zip) works on a *directory*, so the
background task unzips the upload to a temp directory, ingests it, and cleans up.
"""

import logging
import shutil
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, UploadFile, status
from sqlmodel import Session

from app.core.security import require_api_key
from app.db.session import engine
from app.ingest.simplepractice_zip import ingest_export

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["ingest"])


@router.post(
    "/simplepractice-zip",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
)
async def ingest_simplepractice_zip(
    file: UploadFile, background_tasks: BackgroundTasks
) -> dict[str, str]:
    """Accept a SimplePractice export ZIP and schedule background ingestion."""
    fd, tmp_path = tempfile.mkstemp(suffix=".zip")
    zip_path = Path(tmp_path)
    with open(fd, "wb") as tmp_file:
        shutil.copyfileobj(file.file, tmp_file)

    background_tasks.add_task(_run_ingest, zip_path)
    return {
        "status": "accepted",
        "filename": file.filename or "",
        "detail": "ingestion scheduled; poll the patient read endpoints for results",
    }


def _run_ingest(zip_path: Path) -> None:
    """Background task: unzip the upload, ingest it, and clean up the temp files."""
    extract_dir = Path(tempfile.mkdtemp(prefix="sp-export-"))
    try:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(extract_dir)
        with Session(engine) as session:
            summary = ingest_export(extract_dir, session)
            session.commit()
        logger.info("background ingest finished: %s", summary)
    except Exception:
        logger.exception("background ingest failed for %s", zip_path.name)
    finally:
        zip_path.unlink(missing_ok=True)
        shutil.rmtree(extract_dir, ignore_errors=True)
