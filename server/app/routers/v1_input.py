"""Local input confirmation; no unconfigured ASR provider receives audio."""

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from ..services.math_grading import normalize_expression, expressions_equivalent
from ..v1_deps import get_platform_user

router = APIRouter(prefix="/api/v1/input", tags=["input-confirmation"])


class Formula(BaseModel):
    model_config = ConfigDict(extra="forbid")
    raw: str = Field(min_length=1, max_length=2048)
    confirmed: str | None = Field(default=None, max_length=2048)


@router.get("/math/templates")
def templates(actor=Depends(get_platform_user)):
    return {"templates": [{"name": "分数", "text": "(a)/(b)"}, {"name": "乘方", "text": "(a)^(n)"},
                          {"name": "平方根", "text": "sqrt(a)", "automatic_grading": False}],
            "confirmation_required": True}


@router.post("/math/confirm")
def confirm_formula(payload: Formula, actor=Depends(get_platform_user)):
    try:
        normalized = normalize_expression(payload.raw)
    except ValueError:
        return {"raw": payload.raw, "normalized": None, "status": "confirmation_required",
                "message": "表达式不在已支持范围，请用公式面板修正，不自动猜测。"}
    return {"raw": payload.raw, "normalized": normalized, "confirmed": payload.confirmed,
            "status": "confirmed" if payload.confirmed is not None else "confirmation_required",
            "supported_for_programmatic_check": expressions_equivalent(normalized, normalized) is True}


@router.post("/audio/transcriptions")
async def transcribe_audio(file: UploadFile = File(...), actor=Depends(get_platform_user)):
    # Reading a bounded prefix detects oversized input without retaining audio.
    content = await file.read(2 * 1024 * 1024 + 1)
    await file.close()
    if len(content) > 2 * 1024 * 1024:
        raise HTTPException(status_code=413, detail={"code": "audio_too_large", "message": "短音频超过本机限制"})
    raise HTTPException(status_code=503, detail={"code": "asr_not_configured", "message": "语音转写供应商尚未配置和实测，请使用文字或公式输入。音频未保存或外发。"})
