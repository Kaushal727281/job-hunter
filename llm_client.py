"""
llm_client.py
Multi-key LLM client with automatic key rotation.

Priority order:
  1. GROQ_API_KEY  (primary Groq key)
  2. GROQ_API_KEY_2, GROQ_API_KEY_3 … (extra Groq keys — add as many as you like in .env)
  3. GEMINI_API_KEY  (Google Gemini 1.5 Flash — free 1M tokens/day, 1500 req/day)

When a Groq key hits its daily token limit (TPD 429) the next key is tried automatically.
Per-minute 429s are retried once with a short sleep on the same key.
"""

import os
import re
import time
import logging

logger = logging.getLogger(__name__)

GROQ_MODEL   = "llama-3.3-70b-versatile"
GEMINI_MODEL = "gemini-1.5-flash"


def _groq_keys() -> list[str]:
    """Collect GROQ_API_KEY, GROQ_API_KEY_2, GROQ_API_KEY_3 … from env."""
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
    """Extract retry-after seconds from a 429 error string, capped at 20s."""
    m = re.search(r"try again in ([\d.]+)s", err_str, re.I)
    return min(float(m.group(1)) if m else 10.0, 20.0)


def chat_complete(prompt: str, max_tokens: int = 6000, temperature: float = 0.5) -> tuple[str, str]:
    """
    Run a chat completion with automatic key rotation.
    Returns (response_text, finish_reason).
    Raises EnvironmentError if all providers are exhausted.
    """
    groq_keys = _groq_keys()

    for idx, api_key in enumerate(groq_keys):
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
                logger.warning(f"  {label} daily limit reached — trying next key…")
                continue                              # rotate to next key

            if "429" in err:
                wait = _retry_wait(err)
                logger.warning(f"  {label} rate-limit, retrying in {wait:.0f}s…")
                time.sleep(wait)
                try:
                    resp = client.chat.completions.create(
                        model=GROQ_MODEL,
                        messages=[{"role": "user", "content": prompt}],
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )
                    return resp.choices[0].message.content.strip(), resp.choices[0].finish_reason
                except Exception as e2:
                    err2 = str(e2)
                    if "429" in err2 and _is_daily_limit(err2):
                        logger.warning(f"  {label} daily limit on retry — trying next key…")
                        continue
                    raise

            raise   # non-429 error — propagate immediately

    # ── All Groq keys exhausted → try Gemini ─────────────────────────────────
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if gemini_key:
        logger.info("  All Groq keys exhausted — falling back to Gemini 1.5 Flash")
        return _gemini_complete(prompt, gemini_key, max_tokens, temperature)

    raise EnvironmentError(
        "All Groq API keys have hit their daily token limit and no GEMINI_API_KEY is set.\n"
        "Options:\n"
        "  • Add GROQ_API_KEY_2=... to .env (create a free account at console.groq.com)\n"
        "  • Add GEMINI_API_KEY=... to .env (free at aistudio.google.com — 1M tokens/day)"
    )


def _gemini_complete(prompt: str, api_key: str, max_tokens: int, temperature: float) -> tuple[str, str]:
    """Google Gemini 1.5 Flash — free tier: 1,000,000 tokens/day, 1500 req/day."""
    try:
        import google.generativeai as genai
    except ImportError:
        raise ImportError(
            "google-generativeai not installed. Run: pip install google-generativeai"
        )
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        GEMINI_MODEL,
        generation_config=genai.GenerationConfig(
            max_output_tokens=max_tokens,
            temperature=temperature,
        ),
    )
    resp = model.generate_content(prompt)
    return resp.text.strip(), "stop"
