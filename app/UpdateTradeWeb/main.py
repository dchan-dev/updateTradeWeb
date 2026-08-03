from typing import Any
from collections import OrderedDict
import json
import os
import re
import sys
from strands import Agent
import boto3
from strands.agent.conversation_manager.null_conversation_manager import NullConversationManager
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from model.load import load_model


def _configure_utf8_stdio() -> None:
    """Allow Chinese/Japanese/Korean/Tagalog characters on Windows consoles (cp1252)."""
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


_configure_utf8_stdio()

app = BedrockAgentCoreApp()
log = app.logger

AGENTCORE_RUNTIME_REGION = os.getenv("AWS_REGION", "ap-northeast-1")
S3_OUTPUT_BUCKET = os.getenv("DESKPULSE_S3_BUCKET", "deskpulse-public-prod")
S3_LATEST_KEY = os.getenv("DESKPULSE_S3_LATEST_KEY", "daily/latest.json")
_s3_client = boto3.client("s3", region_name=AGENTCORE_RUNTIME_REGION)

REQUIRED_LOCALES = ("en", "zh", "zh-cn", "ja", "ko", "tl")
S3_LATEST_URI = f"s3://{S3_OUTPUT_BUCKET}/{S3_LATEST_KEY}"
# Nova models can fail ConverseStream ToolUse on large JSON arguments.
# Publish is therefore handled in the runtime after the model returns JSON text.
AGENT_TOOLS: list[Any] = []

CREATE_SUMMARY_PROMPT = """
## CREATE SUMMARY (required)
Convert trader execution history into the company-website daily execution message.

### Runtime input (example shape)
Plain-text Buy-In / Sell-Out execution notes, for example:
- Execution Time, Execution ID, Client Account
- Asset Identifier, Transaction Side, Executed Quantity, Execution Price, Venue
- Fee breakdown, TOTAL DEBIT NET / TOTAL CREDIT NET, Settlement Date

### Runtime output (website shape)
Write `latest.json` so the company website can render cards like:

Pen A. C.
Pure-Alpha Portfolio Manager
Expertise: Pure-Alpha Portfolio
quoteHtml with the manager statement
Achievement + comparisonLabel/comparisonValue (for example Compared to the S&amp;P500)

and featured stories that include Trading Execution Summary list rows.

### Input -> output mapping
1. Redact PII from the notes before any website wording:
   - remove Client Account values (example: ACC-HK-99821)
   - remove Execution ID values (example: EXE-HKB-882019, EXE-HKS-885432)
   - remove company legal names when not needed (example: Tencent Holdings Ltd)
   - keep public instrument codes when useful (example: 0700.HK)
2. Remove poor language; upgrade remaining facts to desk-side language.
3. Populate `en.featured.lead` in website card style:
   - author.name / title / expertise (use provided manager profile if present;
     otherwise keep the established Pure-Alpha lead profile style)
   - quoteHtml for the shareholder-facing statement
   - achievement from supported performance facts (trade result and/or NAV vs benchmark
     when present in the source)
4. Populate at least one `en.featured.stories[]` item from the execution cycle:
   - storyTitle
   - bodiesHtml explaining open -> close / buy-in -> sell-out in institutional language
   - quoteHtml
   - details[0].title = "Trading Execution Summary"
   - details[0].lists[0].type = "ul"
   - details[0].lists[0].items labels such as:
     Instrument, Transaction side / Position, Entry price, Exit price,
     Executed quantity, Venue, Return captured, Execution decision,
     Risk observations / Process enhancement when supported
5. If buy and sell nets are both present, compute realized result from the notes
   (credit net minus debit net, and approximate percent on debit net). Do not invent
   unrelated market signals.
6. Keep `clients` tree present for all locales (translate existing/client-safe content;
   do not invent private client identities from execution tickets).

Rules:
- Build English (`en`) content first.
- Prefer absolute-return and process language over promotional hype.
- Website HTML may use <strong> for key numbers and `&amp;` for ampersands.
"""

TRANSLATE_SIX_LANGUAGES_PROMPT = """
## TRANSLATE TO SIX LANGUAGES (required)
After the English summary is complete, translate the full `featured` and `clients`
trees into exactly these six top-level locales:

1. `en` - English
2. `zh` - Taiwan Chinese (Traditional)
3. `zh-cn` - Mainland Chinese (Simplified)
4. `ja` - Japanese
5. `ko` - Korean
6. `tl` - Tagalog

Rules:
- Keep financial terms accurate and natural for each locale.
- Do not mix locales.
- Keep numbers, tickers, index levels, percentages, and dates consistent.
- Escape ampersands in HTML text as `&amp;`.
- Every locale must include both `featured` and `clients`.
"""

PUBLISH_S3_PROMPT = f"""
## PUBLISH latest.json TO S3 (required)
Do not call tools.
Return the complete six-locale JSON object as the final assistant message.
The AgentCore runtime will validate it and store it to:
Target object: `{S3_LATEST_URI}`
Bucket: `{S3_OUTPUT_BUCKET}`
Key: `{S3_LATEST_KEY}`
"""

_LATEST_JSON_SCHEMA = """
## Required latest.json format
Top-level object keys MUST be exactly these locales:
`en`, `zh`, `zh-cn`, `ja`, `ko`, `tl`

Each locale object MUST match:
{
  "featured": {
    "title": "string",
    "subtitle": "string",
    "lead": {
      "author": {
        "initials": "string",
        "name": "string",
        "title": "string",
        "expertise": "string"
      },
      "storyTitle": "string or null",
      "bodiesHtml": ["html paragraph", "..."],
      "quoteHtml": "quoted html string",
      "details": [],
      "achievement": {
        "title": "string",
        "descriptionHtml": "html string",
        "comparisonLabel": "string",
        "comparisonValue": "string"
      }
    },
    "stories": [
      {
        "author": {
          "initials": "string",
          "name": "string",
          "title": "string",
          "expertise": "string"
        },
        "storyTitle": "string or null",
        "bodiesHtml": ["html paragraph", "..."],
        "quoteHtml": "quoted html string",
        "details": [
          {
            "title": "Trading Execution Summary",
            "descriptions": [],
            "lists": [
              {
                "type": "ul",
                "items": [
                  { "label": "Instrument", "html": "..." }
                ]
              }
            ]
          }
        ],
        "achievement": {
          "title": "Achievement",
          "descriptionHtml": "html string",
          "comparisonLabel": "Compared to the S&amp;P500",
          "comparisonValue": "(-4.51%)"
        }
      }
    ]
  },
  "clients": {
    "title": "string",
    "subtitle": "string",
    "stories": [
      {
        "author": {
          "initials": "string",
          "name": "string",
          "title": "string",
          "expertise": "string",
          "experienceYears": "optional string",
          "experienceAreas": "optional string"
        },
        "storyTitle": "string or null",
        "bodiesHtml": ["html paragraph", "..."],
        "quoteHtml": "quoted html string",
        "details": [],
        "results": [
          { "number": "+5%", "label": "Absolute Return" }
        ],
        "achievement": null
      }
    ]
  }
}

Rules for nested objects:
- `achievement` may be an object or null.
- detail `lists[].type` must be `ul` or `ol`.
- list items may include `label` and must include `html`.
- `clients.stories` may include `results` arrays.
- Do not add wrapper keys such as version, source, desk_language, or performance_summary.
- Publish the full website payload for all six locales in one object.

## Output rules
- Do not call tools.
- Final assistant message must be the JSON object only.
- Never wrap JSON in markdown code fences.
- The runtime publishes your JSON to S3 after the response completes.
"""

DEFAULT_SYSTEM_PROMPT = (
    "You are UpdateTradeWeb, an Amazon Bedrock AgentCore runtime that prepares the daily\n"
    "trading-desk execution message for the company website used by shareholders and partners.\n"
    "\n"
    "## Mission\n"
    "Transform trader-provided yesterday trading desk execution history records into a\n"
    "shareholder-safe, desk-quality, multilingual daily execution message, then publish\n"
    "`latest.json` to Amazon S3 for the company website GET API.\n"
    "\n"
    "## Mandatory pipeline\n"
    "You must always run these three jobs in order:\n"
    "1. CREATE SUMMARY\n"
    "2. TRANSLATE TO SIX LANGUAGES\n"
    f"3. STORE latest.json to {S3_LATEST_URI}\n"
    "\n"
    f"{CREATE_SUMMARY_PROMPT}\n"
    f"{TRANSLATE_SIX_LANGUAGES_PROMPT}\n"
    f"{PUBLISH_S3_PROMPT}\n"
    "## Privacy and safety\n"
    "- Strip emails, phone numbers, national IDs, and internal account identifiers.\n"
    "- Replace client/company names with generic labels or omit them.\n"
    "- Do not include brokerage login details, settlement instructions, or internal desk gossip.\n"
    "- Keep only information appropriate for shareholder and partnership communication.\n"
    "\n"
    f"{_LATEST_JSON_SCHEMA}"
)


def _serialize_json_text(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _put_latest_json_to_s3(payload: dict[str, Any]) -> str:
    body = _serialize_json_text(payload)
    _s3_client.put_object(
        Bucket=S3_OUTPUT_BUCKET,
        Key=S3_LATEST_KEY,
        Body=body.encode("utf-8"),
        ContentType="application/json; charset=utf-8",
        CacheControl="no-cache",
    )
    uri = f"s3://{S3_OUTPUT_BUCKET}/{S3_LATEST_KEY}"
    log.info("Uploaded daily execution latest.json to %s", uri)
    return uri


def _extract_json_object(text: str) -> dict[str, Any] | None:
    candidate = text.strip()
    if not candidate:
        return None

    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", candidate, flags=re.DOTALL)
    if fenced:
        candidate = fenced.group(1).strip()

    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def _validate_author(author: Any, path: str) -> str | None:
    if not isinstance(author, dict):
        return f"{path} must be an object"
    for key in ("initials", "name", "title", "expertise"):
        if not isinstance(author.get(key), str) or not author.get(key, "").strip():
            return f"{path}.{key} must be a non-empty string"
    return None


def _validate_story(story: Any, path: str, *, allow_results: bool = False) -> str | None:
    if not isinstance(story, dict):
        return f"{path} must be an object"

    author_error = _validate_author(story.get("author"), f"{path}.author")
    if author_error:
        return author_error

    if "storyTitle" not in story:
        return f"{path}.storyTitle is required (string or null)"
    if story.get("storyTitle") is not None and not isinstance(story.get("storyTitle"), str):
        return f"{path}.storyTitle must be a string or null"

    if not isinstance(story.get("bodiesHtml"), list):
        return f"{path}.bodiesHtml must be an array"
    if not isinstance(story.get("quoteHtml"), str):
        return f"{path}.quoteHtml must be a string"
    if not isinstance(story.get("details"), list):
        return f"{path}.details must be an array"

    achievement = story.get("achievement")
    if achievement is not None:
        if not isinstance(achievement, dict):
            return f"{path}.achievement must be an object or null"
        for key in ("title", "descriptionHtml", "comparisonLabel", "comparisonValue"):
            if not isinstance(achievement.get(key), str):
                return f"{path}.achievement.{key} must be a string"

    if allow_results and "results" in story and story.get("results") is not None:
        results = story.get("results")
        if not isinstance(results, list):
            return f"{path}.results must be an array"
        for index, result in enumerate(results):
            if not isinstance(result, dict):
                return f"{path}.results[{index}] must be an object"
            if not isinstance(result.get("number"), str) or not isinstance(result.get("label"), str):
                return f"{path}.results[{index}] requires string number and label"

    return None


def _validate_locale_payload(locale_payload: Any, locale: str) -> str | None:
    if not isinstance(locale_payload, dict):
        return f"{locale} must be an object"

    featured = locale_payload.get("featured")
    if not isinstance(featured, dict):
        return f"{locale}.featured must be an object"
    for key in ("title", "subtitle"):
        if not isinstance(featured.get(key), str) or not featured.get(key, "").strip():
            return f"{locale}.featured.{key} must be a non-empty string"
    lead_error = _validate_story(featured.get("lead"), f"{locale}.featured.lead")
    if lead_error:
        return lead_error
    stories = featured.get("stories")
    if not isinstance(stories, list):
        return f"{locale}.featured.stories must be an array"
    for index, story in enumerate(stories):
        story_error = _validate_story(story, f"{locale}.featured.stories[{index}]")
        if story_error:
            return story_error

    clients = locale_payload.get("clients")
    if not isinstance(clients, dict):
        return f"{locale}.clients must be an object"
    for key in ("title", "subtitle"):
        if not isinstance(clients.get(key), str) or not clients.get(key, "").strip():
            return f"{locale}.clients.{key} must be a non-empty string"
    client_stories = clients.get("stories")
    if not isinstance(client_stories, list):
        return f"{locale}.clients.stories must be an array"
    for index, story in enumerate(client_stories):
        story_error = _validate_story(
            story,
            f"{locale}.clients.stories[{index}]",
            allow_results=True,
        )
        if story_error:
            return story_error

    return None


_BLOCKED_PII_PATTERNS = (
    re.compile(r"\bACC-[A-Z0-9-]+\b", re.IGNORECASE),
    re.compile(r"\bEXE-[A-Z0-9-]+\b", re.IGNORECASE),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
)


def _collect_strings(value: Any, sink: list[str]) -> None:
    if isinstance(value, str):
        sink.append(value)
        return
    if isinstance(value, list):
        for item in value:
            _collect_strings(item, sink)
        return
    if isinstance(value, dict):
        for item in value.values():
            _collect_strings(item, sink)


def _validate_no_blocked_pii(payload: dict[str, Any]) -> str | None:
    strings: list[str] = []
    _collect_strings(payload, strings)
    joined = "\n".join(strings)
    for pattern in _BLOCKED_PII_PATTERNS:
        match = pattern.search(joined)
        if match:
            return f"blocked PII pattern found in output: {match.group(0)}"
    return None


def _validate_latest_payload(payload: dict[str, Any]) -> str | None:
    missing = [locale for locale in REQUIRED_LOCALES if locale not in payload]
    if missing:
        return f"missing top-level locales: {', '.join(missing)}"

    for locale in REQUIRED_LOCALES:
        locale_error = _validate_locale_payload(payload.get(locale), locale)
        if locale_error:
            return locale_error

    pii_error = _validate_no_blocked_pii(payload)
    if pii_error:
        return pii_error
    return None


_INLINE_FUNCTION_NAMES = set()


def _make_conversation_manager():
    return NullConversationManager()


def _create_agent() -> Agent:
    """Create a tool-free agent. S3 publish is runtime-owned to avoid Nova ToolUse failures."""
    return Agent(
        model=load_model(),
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        tools=AGENT_TOOLS,
        conversation_manager=_make_conversation_manager(),
        # Disable PrintingCallbackHandler: Windows cp1252 cannot print CJK characters
        # such as \u7d14 (純) and would raise UnicodeEncodeError mid-stream.
        callback_handler=None,
        hooks=[],
    )


# Reuses one Agent per session_id so each session keeps its own in-process
# conversation history (best-effort; resets on cold start). The cache is bounded
# to 128 sessions with LRU eviction (least-recently-used is dropped and its
# history reset) so a single process serving many sessions cannot leak history
# between them or grow without limit. For durable history, attach a session manager.
def agent_factory():
    cache = OrderedDict()
    def get_or_create_agent(session_id):
        if session_id in cache:
            cache.move_to_end(session_id)
            return cache[session_id]
        if len(cache) >= 128:
            cache.popitem(last=False)
        cache[session_id] = _create_agent()
        return cache[session_id]
    return get_or_create_agent
get_or_create_agent = agent_factory()


def _task_instruction_prefix() -> str:
    return (
        "Follow the mandatory pipeline for this request:\n"
        "1) CREATE SUMMARY from the trader execution history into website latest.json cards "
        "(featured.lead + featured.stories Trading Execution Summary).\n"
        "2) TRANSLATE TO SIX LANGUAGES: en, zh, zh-cn, ja, ko, tl.\n"
        f"3) Return the complete latest.json object. Runtime will STORE it to {S3_LATEST_URI}.\n"
        "Do not call tools. Redact Client Account / Execution ID / private company names.\n"
        "\n"
        "Trader execution history:\n"
    )


def _with_task_instructions(prompt: Any) -> Any:
    """Ensure every invoke reinforces summary -> 6-language translate -> S3 publish."""
    prefix = _task_instruction_prefix()

    if isinstance(prompt, str):
        if "CREATE SUMMARY" in prompt and "TRANSLATE TO SIX LANGUAGES" in prompt:
            return prompt
        return prefix + prompt

    if isinstance(prompt, list):
        updated: list[Any] = []
        injected = False
        for message in prompt:
            if (
                not injected
                and isinstance(message, dict)
                and message.get("role") == "user"
                and isinstance(message.get("content"), list)
            ):
                content = list(message["content"])
                text_indexes = [
                    index
                    for index, block in enumerate(content)
                    if isinstance(block, dict) and isinstance(block.get("text"), str)
                ]
                if text_indexes:
                    first_text_index = text_indexes[0]
                    original_text = content[first_text_index]["text"]
                    if "CREATE SUMMARY" not in original_text:
                        content[first_text_index] = {
                            **content[first_text_index],
                            "text": prefix + original_text,
                        }
                    injected = True
                    updated.append({**message, "content": content})
                    continue
            updated.append(message)
        return updated

    return prompt


def _extract_prompt(payload: dict):
    """Accept harness-style messages[], tool_results[], or plain prompt string payloads."""
    if "messages" in payload:
        return _with_task_instructions(payload["messages"])
    if "tool_results" in payload:
        return [{"role": "user", "content": [{"toolResult": {
            "toolUseId": tr["toolUseId"],
            "status": tr.get("status", "success"),
            "content": tr.get("content", []),
        }} for tr in payload["tool_results"]]}]
    return _with_task_instructions(payload.get("prompt", ""))


def _has_inline_function_call(messages) -> bool:
    """Return True if messages contains an assistant toolUse for an inline function tool."""
    if not _INLINE_FUNCTION_NAMES or not isinstance(messages, list):
        return False
    for msg in messages:
        if msg.get("role") == "assistant":
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("toolUse", {}).get("name") in _INLINE_FUNCTION_NAMES:
                    return True
    return False


def _is_inline_function_call(event: dict) -> bool:
    """Check if a contentBlockStart event is for an inline function tool."""
    if not _INLINE_FUNCTION_NAMES:
        return False
    cbs = event.get("contentBlockStart", {})
    start = cbs.get("start", {})
    tool_use = start.get("toolUse") if isinstance(start, dict) else None
    return tool_use is not None and tool_use.get("name") in _INLINE_FUNCTION_NAMES


def _append_stream_text(event: dict, text_chunks: list[str]) -> None:
    delta = (
        event.get("event", {})
        .get("contentBlockDelta", {})
        .get("delta", {})
        .get("text")
    )
    if isinstance(delta, str) and delta:
        text_chunks.append(delta)


def _publish_from_response_text(response_text: str) -> str:
    parsed = _extract_json_object(response_text)
    if parsed is None:
        message = "No latest.json object found in agent response; skipping S3 upload"
        log.warning(message)
        return message
    validation_error = _validate_latest_payload(parsed)
    if validation_error:
        message = f"latest.json failed validation: {validation_error}"
        log.warning(message)
        return message
    try:
        uri = _put_latest_json_to_s3(parsed)
    except Exception as exc:
        log.exception("Upload of latest.json failed")
        return f"Failed to upload latest.json: {exc}"
    return f"Published daily execution message to {uri}"



@app.entrypoint
async def invoke(payload, context):
    log.info(
        "Invoking UpdateTradeWeb pipeline: CREATE SUMMARY -> TRANSLATE TO SIX LANGUAGES -> STORE %s",
        S3_LATEST_URI,
    )


    # Fresh tool-free agent each invoke: avoids Nova ToolUse stream errors from
    # large latest.json tool args and avoids stale cached tool definitions.
    _ = getattr(context, "session_id", "default-session")
    agent = _create_agent()

    prompt = _extract_prompt(payload)
    response_chunks: list[str] = []


    async for event in agent.stream_async(
        prompt,
    ):
        if not isinstance(event, dict) or "event" not in event:
            continue
        _append_stream_text(event, response_chunks)
        cbs = event["event"].get("contentBlockStart")
        if cbs is not None and not cbs.get("start"):
            continue
        yield event

    publish_status = _publish_from_response_text("".join(response_chunks))
    log.info("S3 publish status: %s", publish_status)


if __name__ == "__main__":
    app.run()
