"""
llm_client.py
Multi-key LLM client with automatic key rotation.

Priority order:
  1. GROQ_API_KEY, GROQ_API_KEY_2, GROQ_API_KEY_3 … (free 100K tokens/day each)
  2. OLLAMA_MODEL  (local, no internet, no limits — install ollama.com then: ollama pull llama3.1:8b)
  3. DEEPSEEK_API_KEY   (free credits — platform.deepseek.com)
  4. MISTRAL_API_KEY    (free credits — console.mistral.ai)
  5. OPENROUTER_API_KEY (free models — openrouter.ai, works in India)
  6. GEMINI_API_KEY     (free 1M tokens/day — aistudio.google.com/apikey)

Set OLLAMA_MODEL=llama3.1:8b in .env to use local Ollama (best for no-internet/no-limit use).
When a Groq key hits its daily token limit the next provider is tried automatically.
"""

import os
import re
import time
import logging
import truststore

truststore.inject_into_ssl()
logger = logging.getLogger(__name__)

GROQ_MODEL       = "llama-3.3-70b-versatile"
DEEPSEEK_MODEL   = "deepseek-chat"
MISTRAL_MODEL    = "mistral-small-latest"
OPENROUTER_MODEL = "meta-llama/llama-3.3-70b-instruct:free"
GEMINI_MODEL     = "gemini-1.5-flash"
OLLAMA_BASE_URL  = "http://localhost:11434/v1"


def _groq_keys() -> list[str]:
    keys = []
    k = os.getenv("GROQ_API_KEY", "").strip()
    if k:
        keys.append(k)
    i = 2
    while True:
        k = os.getenv(f"GROQ_API_KEY_{i}", "").strip()
        if not k:
            break
        keys.append(k)
        i += 1
    return keys


def _is_daily_limit(err_str: str) -> bool:
    return "tokens per day" in err_str or "TPD" in err_str or "per day" in err_str.lower()


def _retry_wait(err_str: str) -> float:
    m = re.search(r"try again in ([\d.]+)s", err_str, re.I)
    return min(float(m.group(1)) if m else 10.0, 20.0)


def _openai_compat(api_key: str, base_url: str, model: str,
                   prompt: str, max_tokens: int, temperature: float,
                   extra_headers: dict = None) -> tuple[str, str]:
    """Generic OpenAI-compatible call (used by DeepSeek, Mistral, OpenRouter)."""
    from openai import OpenAI
    kwargs = dict(api_key=api_key, base_url=base_url)
    if extra_headers:
        kwargs["default_headers"] = extra_headers
    client = OpenAI(**kwargs)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return resp.choices[0].message.content.strip(), resp.choices[0].finish_reason


def chat_complete(prompt: str, max_tokens: int = 6000, temperature: float = 0.5) -> tuple[str, str]:
    """
    Run a chat completion with automatic key rotation.
    Returns (response_text, finish_reason).
    Raises EnvironmentError if all providers are exhausted.
    """
    # ── 1. Groq (multiple keys) ───────────────────────────────────────────────
    for idx, api_key in enumerate(_groq_keys()):
        label = f"Groq key #{idx + 1}"
        try:
            from groq import Groq
            client = Groq(api_key=api_key)
            resp = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=temperature,
            )
            if idx > 0:
                logger.info(f"  Used {label}")
            return resp.choices[0].message.content.strip(), resp.choices[0].finish_reason
        except Exception as e:
            err = str(e)
            if "429" in err and _is_daily_limit(err):
                logger.warning(f"  {label} daily limit — trying next…")
                continue
            if "429" in err:
                wait = _retry_wait(err)
                logger.warning(f"  {label} rate-limit, retrying in {wait:.0f}s…")
                time.sleep(wait)
                try:
                    resp = client.chat.completions.create(
                        model=GROQ_MODEL,
                        messages=[{"role": "user", "content": prompt}],
                        max_tokens=max_tokens, temperature=temperature,
                    )
                    return resp.choices[0].message.content.strip(), resp.choices[0].finish_reason
                except Exception as e2:
                    if "429" in str(e2) and _is_daily_limit(str(e2)):
                        logger.warning(f"  {label} daily limit on retry — trying next…")
                        continue
                    raise
            raise

    # ── 2. Ollama (local, no internet, no limits) ─────────────────────────────
    ollama_model = os.getenv("OLLAMA_MODEL", "").strip()
    if ollama_model:
        logger.info(f"  Groq exhausted — trying Ollama ({ollama_model})")
        try:
            return _openai_compat("ollama", OLLAMA_BASE_URL, ollama_model,
                                  prompt, max_tokens, temperature)
        except Exception as e:
            logger.warning(f"  Ollama failed: {e} — trying next provider…")

    # ── 3. DeepSeek ───────────────────────────────────────────────────────────
    k = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if k:
        logger.info("  Groq exhausted — trying DeepSeek")
        return _openai_compat(k, "https://api.deepseek.com", DEEPSEEK_MODEL,
                              prompt, max_tokens, temperature)

    # ── 3. Mistral ────────────────────────────────────────────────────────────
    k = os.getenv("MISTRAL_API_KEY", "").strip()
    if k:
        logger.info("  Trying Mistral")
        return _openai_compat(k, "https://api.mistral.ai/v1", MISTRAL_MODEL,
                              prompt, max_tokens, temperature)

    # ── 4. OpenRouter (has permanently free models) ───────────────────────────
    k = os.getenv("OPENROUTER_API_KEY", "").strip()
    if k:
        logger.info("  Trying OpenRouter (free model)")
        headers = {"HTTP-Referer": "https://github.com/job-hunter", "X-Title": "Job Hunter"}
        # OpenRouter free models sometimes get upstream throttled — retry up to 3x
        for attempt in range(3):
            try:
                return _openai_compat(k, "https://openrouter.ai/api/v1", OPENROUTER_MODEL,
                                      prompt, max_tokens, temperature, extra_headers=headers)
            except Exception as e:
                err = str(e)
                if "429" in err and attempt < 2:
                    # Extract retry_after from metadata if present, else default 30s
                    m = re.search(r"retry_after_seconds['\"]:\s*([\d.]+)", err)
                    wait = min(float(m.group(1)) if m else 30.0, 60.0)
                    logger.warning(f"  OpenRouter upstream throttle, retrying in {wait:.0f}s…")
                    time.sleep(wait)
                else:
                    raise

    # ── 5. Gemini ─────────────────────────────────────────────────────────────
    k = os.getenv("GEMINI_API_KEY", "").strip()
    if k:
        logger.info("  Trying Gemini 1.5 Flash")
        return _gemini_complete(prompt, k, max_tokens, temperature)

    raise EnvironmentError(
        "All LLM providers exhausted or not configured. Add one of these to .env:\n"
        "  OLLAMA_MODEL=llama3.1:8b   local, free, no limits (install from ollama.com)\n"
        "  GROQ_API_KEY_2=...         console.groq.com        (100K/day free)\n"
        "  MISTRAL_API_KEY=...        console.mistral.ai      (free credits)\n"
        "  OPENROUTER_API_KEY=...     openrouter.ai           (free models)\n"
        "  DEEPSEEK_API_KEY=...       platform.deepseek.com   (free credits)\n"
        "  GEMINI_API_KEY=...         aistudio.google.com     (1M/day free)"
    )


def _gemini_complete(prompt: str, api_key: str, max_tokens: int, temperature: float) -> tuple[str, str]:
    try:
        import google.generativeai as genai
    except ImportError:
        raise ImportError("Run: pip install google-generativeai")
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        GEMINI_MODEL,
        generation_config=genai.GenerationConfig(
            max_output_tokens=max_tokens, temperature=temperature,
        ),
    )
    resp = model.generate_content(prompt)
    return resp.text.strip(), "stop"
