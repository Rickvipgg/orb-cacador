import os
from pathlib import Path

from supabase import create_client

ROOT = Path(__file__).resolve().parent
STATE_BUCKET = os.getenv("ORB_STATE_BUCKET", "orb-state")
PANEL_BUCKET = os.getenv("ORB_PANEL_BUCKET", "orb-panel")


def client():
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SECRET_KEY"]
    return create_client(url, key)


def pull_one(sb, bucket, remote_name, local_path):
    try:
        data = sb.storage.from_(bucket).download(remote_name)
        Path(local_path).write_bytes(data)
        print(f"[cloud] baixado {remote_name}")
        return True
    except Exception as e:
        print(f"[cloud] {remote_name} ainda não existe: {e}")
        return False


def push_one(sb, bucket, remote_name, local_path, content_type="application/octet-stream"):
    local_path = Path(local_path)
    if not local_path.exists():
        print(f"[cloud] ignorando ausente: {local_path}")
        return
    with local_path.open("rb") as f:
        sb.storage.from_(bucket).upload(
            path=remote_name,
            file=f,
            file_options={"upsert": "true", "content-type": content_type, "cache-control": "60"},
        )
    print(f"[cloud] enviado {remote_name}")


def pull_state():
    sb = client()
    pull_one(sb, STATE_BUCKET, "historico.sqlite", ROOT / "historico.sqlite")
    pull_one(sb, STATE_BUCKET, "feed_status.json", ROOT / "feed_status.json")


def push_state():
    sb = client()
    push_one(sb, STATE_BUCKET, "historico.sqlite", ROOT / "historico.sqlite", "application/x-sqlite3")
    push_one(sb, STATE_BUCKET, "feed_status.json", ROOT / "feed_status.json", "application/json")
    push_one(sb, PANEL_BUCKET, "latest.json", ROOT / "saida" / "latest.json", "application/json")
