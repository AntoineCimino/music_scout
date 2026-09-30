"""LLM access via the `claude` CLI (Pro Plan, no API key). Returns None on any failure."""
import json
import re
import shutil
import subprocess


def ask_json(prompt, system="Reply with valid JSON only.", model="haiku", timeout=180):
    """Run one `claude -p` call and parse a JSON object/array from its reply; None if unavailable."""
    if not shutil.which("claude"):
        return None
    try:
        out = subprocess.run(
            ["claude", "-p", prompt, "--model", model, "--output-format", "json", "--system-prompt", system],
            capture_output=True, text=True, check=True, timeout=timeout,
        ).stdout
        text = json.loads(out).get("result") or ""
        m = re.search(r"[\[{].*[\]}]", text, re.S)  # tolerate ```json fences / prose around it
        return json.loads(m.group(0)) if m else None
    except (subprocess.SubprocessError, OSError, ValueError, AttributeError):
        return None
