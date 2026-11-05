"""Make one Sarvam chat-completion request using SARVAM_API_KEY from the environment."""

import os

from dotenv import load_dotenv
from sarvamai import SarvamAI


def main() -> int:
    load_dotenv()
    api_key = os.environ.get("SARVAM_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("SARVAM_API_KEY is required in the environment or .env file")

    client = SarvamAI(api_subscription_key=api_key, timeout=60.0)
    response = client.chat.completions(
        model="sarvam-105b-conversations",
        messages=[{"role": "user", "content": "Reply with a short greeting."}],
        max_tokens=80,
        request_options={"timeout_in_seconds": 60, "max_retries": 0},
    )
    print(response.choices[0].message.content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
