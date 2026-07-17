"""
llm_client.py
Multi-key LLM client with automatic key rotation.

Priority order:
  1. GROQ_API_KEY, GROQ_API_KEY_2, GROQ_API_KEY_3 … (free 100K tokens/day each)
  2. DEEPSEEK_API_KEY   (free credits on signup — platform.deepseek.com)
  3. MISTRAL_API_KEY    (free credits — console.mistral.ai, works in India)
  4. OPENROUTER_API_KEY (FREE models, no credits needed — openrouter.ai, works in India)
  5. GEMINI_API_KEY     (free 1M tokens/day — aistudio.google.com/apikey)

When a Groq key hits its daily token limit the next key is tried automatically.
Per-minute 429s are retried once with a short sleep on the same key.

Getting free keys (all work in India):
  Groq:        https://console.groq.com           100K tokens/day free
  Mistral:     https://console.mistral.ai          free credits on signup
  OpenRouter:  https://openrouter.ai/settings/keys free models available (no credits needed)
  DeepSeek:    https://platform.deepseek.com       free credits on signup
  Gemini:      https://aistudio.google.com/apikey  may need VPN in India
"""

import os
import re
import time
import logging

logger = logging.getLogger(__name__)

GROQ_MODEL       = "llama-3.3-70b-versatile"
DEEPSEEK_MODEL   = "deepseek-chat"
MISTRAL_MODEL    = "mistral-small-latest"
OPENROUTER_MODEL = "meta-llama/llama-3.3-70b-instruct:free"   # permanently free
GEMINI_MODEL     = "gemini-1.5-flash"


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

    # ── 2. DeepSeek ───────────────────────────────────────────────────────────
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
        return _openai_compat(
            k, "https://openrouter.ai/api/v1", OPENROUTER_MODEL,
            prompt, max_tokens, temperature,
            extra_headers={"HTTP-Referer": "https://github.com/job-hunter",
                           "X-Title": "Job Hunter"},
        )

    # ── 5. Gemini ─────────────────────────────────────────────────────────────
    k = os.getenv("GEMINI_API_KEY", "").strip()
    if k:
        logger.info("  Trying Gemini 1.5 Flash")
        return _gemini_complete(prompt, k, max_tokens, temperature)

    raise EnvironmentError(
        "All LLM keys exhausted or not set. Add one of these to .env:\n"
        "  GROQ_API_KEY_2=...      console.groq.com        (100K/day free)\n"
        "  MISTRAL_API_KEY=...     console.mistral.ai      (free credits, works in India)\n"
        "  OPENROUTER_API_KEY=...  openrouter.ai           (free models, works in India)\n"
        "  DEEPSEEK_API_KEY=...    platform.deepseek.com   (free credits)\n"
        "  GEMINI_API_KEY=...      aistudio.google.com     (1M/day free)"
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
