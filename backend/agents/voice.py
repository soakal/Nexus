import logging

logger = logging.getLogger(__name__)


async def transcribe(audio_path: str) -> str:
    from backend.config import get_settings
    settings = get_settings()
    if settings.whisper_api:
        from openai import OpenAI
        try:
            from backend.secrets.manager import get_secret
            openai_key = get_secret("OPENAI_API_KEY")
        except Exception:
            openai_key = ""
        client = OpenAI(api_key=openai_key)
        with open(audio_path, "rb") as f:
            resp = client.audio.transcriptions.create(model="whisper-1", file=f)
        return resp.text
    else:
        import whisper
        model = whisper.load_model(settings.whisper_model)
        result = model.transcribe(audio_path)
        return result["text"]


async def route_intent(transcript: str) -> dict:
    from backend.agents.router import haiku
    prompt = f"""Classify this voice command into one of: TASK, QUERY, BRIEFING, HOME_CONTROL, NOTE

Voice transcript: "{transcript}"

Return JSON only:
{{
  "intent": "TASK|QUERY|BRIEFING|HOME_CONTROL|NOTE",
  "confidence": 0.0-1.0,
  "extracted_action": "what the user wants to do",
  "parameters": {{}}
}}"""
    raw = await haiku(prompt, label="voice_intent")
    import json
    start = raw.find("{")
    end = raw.rfind("}") + 1
    return json.loads(raw[start:end])


async def process_audio(audio_path: str) -> dict:
    transcript = await transcribe(audio_path)
    logger.info(f"Transcribed: {transcript[:100]}")

    intent_data = await route_intent(transcript)
    intent = intent_data.get("intent", "QUERY")

    result = {"transcript": transcript, "intent": intent, "intent_data": intent_data}

    if intent == "BRIEFING":
        from backend.agents.briefing import run_briefing
        briefing = await run_briefing()
        result["response"] = briefing[:500] + "..." if len(briefing) > 500 else briefing

    elif intent == "QUERY":
        from backend.agents.router import sonnet
        resp = await sonnet(f"Answer this question concisely: {intent_data.get('extracted_action', transcript)}")
        result["response"] = resp

    elif intent == "HOME_CONTROL":
        from backend.integrations.homeassistant import call_service
        params = intent_data.get("parameters", {})
        resp = await call_service(params.get("domain", ""), params.get("service", ""), params.get("data", {}))
        result["response"] = f"Home Assistant: {resp}"

    elif intent == "NOTE":
        from backend.integrations.obsidian import create_note
        path = await create_note(title=transcript[:50], content=transcript, folder="NEXUS/Voice Notes")
        result["response"] = f"Note saved to {path}"

    elif intent == "TASK":
        from backend.agents.orchestrator import run_task
        task_result = await run_task(intent_data.get("extracted_action", transcript))
        result["response"] = "Task complete" if task_result.success else f"Task failed: {task_result.reason}"
        result["task_result"] = {"success": task_result.success, "steps": len(task_result.output)}

    return result
