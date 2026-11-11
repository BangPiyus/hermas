"""Create a unique local-demo configuration without embedding sample secrets."""
from pathlib import Path
import os
import secrets

root = Path(__file__).resolve().parent
file = root / ".env"
if file.exists():
    print(".env sudah ada; tidak ditimpa.")
else:
    password = secrets.token_urlsafe(22)
    secret = secrets.token_hex(32)
    text = (
        "# Generated for local DEMO. Save the password printed during setup.\n"
        f"HERMAS_ADMIN_PASSWORD={password}\n"
        f"HERMAS_SESSION_SECRET={secret}\n"
        "HERMAS_MODEL_PROVIDER=demo\n"
        "HERMAS_MODEL=gpt-4.1-mini\n"
        "HERMAS_ALLOWED_HOSTS=localhost,127.0.0.1\n"
        "HERMAS_ENABLE_DOCKER_TESTS=false\n"
        "HERMAS_SECURE_COOKIE=false\n"
        "HERMAS_DAILY_TOKEN_BUDGET=40000\n"
    )
    fd = os.open(file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    print("\nHermas Agentic AI: setup demo berhasil.")
    print("URL: http://127.0.0.1:8765")
    print("ADMIN PASSWORD:", password)
    print("\nSimpan password ini. Untuk AI nyata, ubah .env ke provider openai dan isi API key.")
(root / "workspace").mkdir(exist_ok=True)
