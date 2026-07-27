import os
import tempfile

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from backend.auth import require_api_key

router = APIRouter()

# Whisper transcribes a memo, not a movie: 50 MB is ~1h of 128kbps audio and far
# above any real voice note. Enforced while streaming so an oversized (or
# Content-Length-lying) body is never fully buffered in memory.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_CHUNK = 1024 * 1024


def _checked_suffix(file: UploadFile, allowed: tuple[str, ...], default: str = "") -> str:
    """Validate the upload's extension and return it.

    `filename` is client-supplied and optional in multipart — a missing one used
    to raise AttributeError (500). Only the extension is ever used; the name
    itself never reaches the filesystem (NamedTemporaryFile picks the path).
    """
    name = file.filename or ""
    suffix = os.path.splitext(name)[1].lower()
    if suffix not in allowed:
        raise HTTPException(status_code=400, detail="Unsupported audio format")
    return suffix or default


async def _spool_to_temp(file: UploadFile, suffix: str) -> str:
    written = 0
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = tmp.name
        while chunk := await file.read(_CHUNK):
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                tmp.close()
                os.unlink(tmp_path)
                raise HTTPException(status_code=413, detail="Audio file too large")
            tmp.write(chunk)
    return tmp_path


@router.post("/upload")
async def upload_voice(file: UploadFile = File(...), _=Depends(require_api_key)):
    suffix = _checked_suffix(file, (".wav", ".mp3", ".m4a"))
    tmp_path = await _spool_to_temp(file, suffix)

    try:
        from backend.agents.voice import process_audio
        result = await process_audio(tmp_path)
        return result
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


@router.post("/transcribe")
async def transcribe_voice(file: UploadFile = File(...), _=Depends(require_api_key)):
    suffix = _checked_suffix(file, (".wav", ".mp3", ".m4a", ".webm", ".ogg"), default=".webm")
    tmp_path = await _spool_to_temp(file, suffix)

    try:
        from backend.agents.voice import transcribe
        text = await transcribe(tmp_path)
        return {"transcript": text}
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
