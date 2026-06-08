import os
import tempfile

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from backend.auth import require_api_key

router = APIRouter()


@router.post("/upload")
async def upload_voice(file: UploadFile = File(...), _=Depends(require_api_key)):
    if not file.filename.lower().endswith((".wav", ".mp3", ".m4a")):
        raise HTTPException(status_code=400, detail="Unsupported audio format")

    suffix = os.path.splitext(file.filename)[1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        from backend.agents.voice import process_audio
        result = await process_audio(tmp_path)
        return result
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
