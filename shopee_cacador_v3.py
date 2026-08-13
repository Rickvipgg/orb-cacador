#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import html
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

# Evita erro de encoding de emojis/acentos em Windows e runners de nuvem.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DOWNLOAD_DIR = ROOT / "downloads"
OUTPUT_DIR = ROOT / "saida"
LOG_DIR = ROOT / "logs"
DB_PATH = ROOT / "historico.sqlite"
FEED_STATE_PATH = ROOT / "feed_status.json"

for d in (DOWNLOAD_DIR, OUTPUT_DIR, LOG_DIR):
    d.mkdir(exist_ok=True)

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def fnum(v, default=0.0):
    try:
        if v in ("", None):
            return default
        return float(str(v).replace(",", "."))
    except Exception:
        return default

def fint(v, default=0):
    try:
        return int(float(v))
    except Exception:
        return default

def br_money(v):
    s = f"{float(v):,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")

def clean_tracking(base):
    base = (base or "").strip()
    if not base:
        return ""
    base = re.sub(r"([?&])affiliate_id=[^&]*&?", r"\1", base)
    base = re.sub(r"([?&])sub_id=[^&]*&?", r"\1", base)
    return base.rstrip("?&")

def make_affiliate_link(base, affiliate_id, sub_id):
    base = clean_tracking(base)
    if not base:
        return ""
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}affiliate_id={affiliate_id}&sub_id={sub_id}"


def apply_env_overrides(cfg):
    """Permite guardar credenciais/URLs em secrets sem colocá-las no GitHub."""
    cfg["affiliate_id"] = os.getenv("SHOPEE_AFFILIATE_ID", str(cfg.get("affiliate_id", "")))
    cfg["sub_id"] = os.getenv("SHOPEE_SUB_ID", str(cfg.get("sub_id", "orb-promocoes")))

    for i, feed in enumerate(cfg.get("feeds", []), 1):
        env_url = os.getenv(f"SHOPEE_FEED_{i}_URL")
        if env_url:
            feed["url"] = env_url
    return cfg

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def load_feed_state():
    if not FEED_STATE_PATH.exists():
        return {}
    try:
        with open(FEED_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_feed_state(state):
    with open(FEED_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def evaluate_feed_freshness(feed_name, target, previous_state):
    now = datetime.now()
    file_hash = sha256_file(target)
    size = target.stat().st_size

    old = previous_state.get(feed_name) or {}
    old_hash = old.get("hash")
    old_changed_at = old.get("last_changed_at")

    if not old_hash:
        status = "PRIMEIRA_CAPTURA"
        last_changed_at = now.isoformat(timespec="seconds")
        age_hours = 0.0
    elif old_hash != file_hash:
        status = "NOVO_FEED"
        last_changed_at = now.isoformat(timespec="seconds")
        age_hours = 0.0
    else:
        last_changed_at = old_changed_at or old.get("last_success_at") or now.isoformat(timespec="seconds")
        try:
            changed_dt = datetime.fromisoformat(last_changed_at)
            age_hours = max(0.0, (now - changed_dt).total_seconds() / 3600)
        except Exception:
            age_hours = 0.0

        if age_hours >= 36:
            status = "POSSIVELMENTE_DESATUALIZADO"
        else:
            status = "SEM_MUDANCA"

    return {
        "nome": feed_name,
        "status": status,
        "hash": file_hash,
        "size_bytes": size,
        "last_success_at": now.isoformat(timespec="seconds"),
        "last_changed_at": last_changed_at,
        "age_hours": round(age_hours, 1),
    }

def freshness_label(status):
    return {
        "PRIMEIRA_CAPTURA": "🟦 Primeira captura",
        "NOVO_FEED": "🟢 Feed novo detectado",
        "SEM_MUDANCA": "🟡 Sem mudança desde a última execução",
        "POSSIVELMENTE_DESATUALIZADO": "🔴 Possivelmente desatualizado",
        "ERRO_DOWNLOAD": "🔴 Erro no download",
    }.get(status, status)

def download_feed(url, target):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/csv,application/octet-stream,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=180) as resp, open(target, "wb") as out:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)

def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots(
            itemid TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            sale_price REAL NOT NULL,
            original_price REAL,
            discount REAL,
            PRIMARY KEY(itemid, observed_at)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS served(
            itemid TEXT PRIMARY KEY,
            served_at TEXT NOT NULL,
            price REAL NOT NULL
        )
    """)
    conn.commit()

def historical_stats(conn, itemid, current_price):
    rows = conn.execute(
        """
        SELECT sale_price
        FROM snapshots
        WHERE itemid=?
        ORDER BY observed_at DESC
        LIMIT 30
        """,
        (itemid,),
    ).fetchall()

    prices = [float(x[0]) for x in rows if x[0] and float(x[0]) > 0]
    if not prices:
        return {"samples":0,"avg":0,"min":0,"last":0,"drop_vs_avg":0,"drop_vs_last":0}

    avg = sum(prices) / len(prices)
    minimum = min(prices)
    last = prices[0]
    drop_avg = max(0, (avg - current_price) / avg * 100) if avg else 0
    drop_last = max(0, (last - current_price) / last * 100) if last else 0

    return {
        "samples": len(prices),
        "avg": avg,
        "min": minimum,
        "last": last,
        "drop_vs_avg": round(drop_avg, 1),
        "drop_vs_last": round(drop_last, 1),
    }

def base_score(r):
    disc = fnum(r.get("discount_percentage"))
    ir = fnum(r.get("item_rating"))
    sr = fnum(r.get("shop_rating"), 4.7)
    likes = max(0, fint(r.get("like")))
    sale = fnum(r.get("sale_price"))
    domestic = "Non-Cross" in str(r.get("cb_option") or "")

    discount_pts = min(max(disc, 0) / 50, 1) * 30
    item_pts = min(max((ir - 4.7) / 0.3, 0), 1) * 18
    shop_pts = min(max((sr - 4.7) / 0.3, 0), 1) * 12
    popularity_pts = min(math.log10(likes + 1) / 4, 1) * 15

    if sale <= 50:
        price_pts = 12
    elif sale <= 100:
        price_pts = 10
    elif sale <= 250:
        price_pts = 8
    elif sale <= 500:
        price_pts = 5
    else:
        price_pts = 0

    domestic_pts = 3 if domestic else 0

    return round(
        discount_pts + item_pts + shop_pts + popularity_pts + price_pts + domestic_pts,
        1
    )

def final_score(r, hist):
    base = base_score(r)
    if hist["samples"] == 0:
        return base

    historical = min(hist["drop_vs_avg"] * 2.2 + hist["drop_vs_last"] * 1.5, 100)
    return round(base * 0.78 + historical * 0.22, 1)

def load_items(paths):
    items = {}
    total = 0
    for path in paths:
        log(f"Lendo {path.name}...")
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                total += 1
                itemid = str(r.get("itemid") or "").strip()
                if not itemid:
                    continue
                completeness = sum(v not in ("", None) for v in r.values())
                r["_completeness"] = completeness
                old = items.get(itemid)
                if old is None or completeness > old["_completeness"]:
                    items[itemid] = r
    return items, total

def make_queue_times(cfg):
    start_h, start_m = map(int, cfg["fila"]["inicio"].split(":"))
    end_h, end_m = map(int, cfg["fila"]["fim"].split(":"))
    interval = int(cfg["fila"]["intervalo_minutos"])
    queue_date = (datetime.now() + timedelta(days=1)).date()
    cur = datetime.combine(queue_date, datetime.min.time()).replace(hour=start_h, minute=start_m)
    end = datetime.combine(queue_date, datetime.min.time()).replace(hour=end_h, minute=end_m)
    slots = []
    while cur <= end:
        slots.append(cur)
        cur += timedelta(minutes=interval)
    return slots

def make_copy(r, link, score, hist):
    title = str(r.get("title") or "").strip()
    short = title if len(title) <= 95 else title[:92].rstrip() + "..."
    normal = fnum(r.get("price"))
    sale = fnum(r.get("sale_price"))
    disc = fnum(r.get("discount_percentage"))
    rating = fnum(r.get("item_rating"))
    likes = fint(r.get("like"))

    seed = sum(ord(c) for c in str(r.get("itemid") or title)) % 10
    intros = [
        "🚨 ACHADINHO PESADO",
        "🔥 OLHA ESSE PREÇO",
        "😳 ESSA AQUI TÁ BOA",
        "⚡ PROMO BOA APARECEU",
        "🛒 ACHADO DO DIA",
        "💥 PREÇO DESPENCOU",
        "👀 OLHA O QUE EU ACHEI",
        "🚀 OFERTA PRA APROVEITAR",
        "🤯 ISSO AQUI TÁ BARATO",
        "🔥🔥 CORRE NESSE ACHADO",
    ]
    hist_line = ""
    if hist["samples"] >= 1 and hist["drop_vs_avg"] >= 5:
        hist_line = f"\n📉 {hist['drop_vs_avg']:.0f}% abaixo da média que acompanhamos"

    social = ""
    if likes:
        social = f" | ❤️ {likes:,} curtidas".replace(",", ".")

    return (
        f"{intros[seed]}\n\n"
        f"*{short}*\n\n"
        f"De {br_money(normal)} por *{br_money(sale)}* — {disc:.0f}% OFF\n"
        f"⭐ {rating:.2f}/5{social}"
        f"{hist_line}\n\n"
        f"👉 {link}\n\n"
        f"_Preço e estoque podem mudar a qualquer momento._"
    )

def write_csv(selected, path):
    fields = [
        "hora_sugerida","score","produto","categoria","loja","preco_normal","preco_agora",
        "desconto_pct","nota_produto","nota_loja","likes","queda_vs_media_pct",
        "amostras_historico","copy_pronta","link_afiliado","itemid","observado_em"
    ]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=";")
        w.writeheader()
        for x in selected:
            r = x["row"]
            hist = x["hist"]
            w.writerow({
                "hora_sugerida": x["slot"].strftime("%d/%m %H:%M"),
                "score": x["score"],
                "produto": r.get("title") or "",
                "categoria": r.get("global_category1") or "",
                "loja": r.get("shop_name") or "",
                "preco_normal": br_money(fnum(r.get("price"))),
                "preco_agora": br_money(fnum(r.get("sale_price"))),
                "desconto_pct": f"{fnum(r.get('discount_percentage')):.0f}%",
                "nota_produto": f"{fnum(r.get('item_rating')):.2f}",
                "nota_loja": f"{fnum(r.get('shop_rating')):.2f}",
                "likes": fint(r.get("like")),
                "queda_vs_media_pct": f"{hist['drop_vs_avg']:.1f}%",
                "amostras_historico": hist["samples"],
                "copy_pronta": x["copy"],
                "link_afiliado": x["link"],
                "itemid": r.get("itemid") or "",
                "observado_em": x["observed_at"],
            })

def write_latest_json(selected, path, stats, feed_statuses):
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "stats": {**stats, "queue": len(selected)},
        "feeds": feed_statuses,
        "offers": [],
    }
    for x in selected:
        r = x["row"]
        hist = x["hist"]
        payload["offers"].append({
            "hora_sugerida": x["slot"].strftime("%H:%M"),
            "score": x["score"],
            "itemid": str(r.get("itemid") or ""),
            "produto": str(r.get("title") or ""),
            "categoria": str(r.get("global_category1") or "Outros"),
            "loja": str(r.get("shop_name") or ""),
            "preco_normal": fnum(r.get("price")),
            "preco_agora": fnum(r.get("sale_price")),
            "desconto_pct": fnum(r.get("discount_percentage")),
            "nota_produto": fnum(r.get("item_rating")),
            "nota_loja": fnum(r.get("shop_rating")),
            "likes": fint(r.get("like")),
            "queda_vs_media_pct": hist.get("drop_vs_avg", 0),
            "amostras_historico": hist.get("samples", 0),
            "imagem": str(r.get("image_link") or ""),
            "copy_pronta": x["copy"],
            "link_afiliado": x["link"],
            "observado_em": x["observed_at"],
        })
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

def esc(v):
    return html.escape(str(v), quote=True)

def write_html(selected, path, stats, feed_statuses):
    cards = []
    for x in selected:
        r = x["row"]
        hist = x["hist"]
        itemid = str(r.get("itemid") or "")
        title = str(r.get("title") or "")
        cat = str(r.get("global_category1") or "Outros")
        rating = fnum(r.get("item_rating"))
        likes = fint(r.get("like"))
        normal = fnum(r.get("price"))
        sale = fnum(r.get("sale_price"))
        disc = fnum(r.get("discount_percentage"))
        copy_json = json.dumps(x["copy"], ensure_ascii=False)
        link_json = json.dumps(x["link"], ensure_ascii=False)

        history_badge = ""
        if hist["samples"]:
            history_badge = f'<span class="pill">Histórico: {hist["samples"]}x</span>'
        if hist["drop_vs_avg"] >= 5:
            history_badge += f'<span class="pill good">↓ {hist["drop_vs_avg"]:.0f}% vs média</span>'

        likes_badge = ""
        if likes:
            likes_badge = f'<span class="pill">❤️ {likes:,}</span>'.replace(",", ".")

        card = f"""
        <article class="card" data-id="{esc(itemid)}" data-title="{esc(title.lower())}" data-cat="{esc(cat.lower())}">
          <div class="top">
            <span class="time">{x["slot"].strftime("%H:%M")}</span>
            <span class="score">🔥 {x["score"]:.0f}</span>
          </div>
          <h2>{esc(title)}</h2>
          <div class="meta">
            <span class="pill">{esc(cat)}</span>
            <span class="pill">⭐ {rating:.2f}</span>
            {likes_badge}
            {history_badge}
          </div>
          <div class="price">
            <span class="old">{esc(br_money(normal))}</span>
            <strong>{esc(br_money(sale))}</strong>
            <span class="discount">-{disc:.0f}%</span>
          </div>
          <div class="actions">
            <button class="copy" onclick='copyPost({copy_json})'>📋 COPIAR POST</button>
            <button onclick='openProduct({link_json})'>🔗 ABRIR PRODUTO</button>
            <button class="posted" onclick='togglePosted("{esc(itemid)}", this)'>✓ MARCAR POSTADO</button>
          </div>
        </article>
        """
        cards.append(card)

    generated = datetime.now().strftime("%d/%m/%Y %H:%M")
    body = "\n".join(cards)

    feed_boxes = []
    for fs in feed_statuses:
        label = freshness_label(fs.get("status"))
        changed = fs.get("last_changed_at") or "-"
        try:
            changed_fmt = datetime.fromisoformat(changed).strftime("%d/%m %H:%M")
        except Exception:
            changed_fmt = changed
        age = fs.get("age_hours", 0)
        feed_boxes.append(
            f'<div class="feedstatus"><b>{esc(fs.get("nome","Feed"))}</b>'
            f'{esc(label)}<br>Última mudança detectada: {esc(changed_fmt)}'
            f'<br>Sem mudança há: {esc(age)}h</div>'
        )
    feed_box_html = "".join(feed_boxes)

    doc = f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shopee Caçador V3</title>
<style>
*{{box-sizing:border-box}} body{{margin:0;background:#f5f6f8;color:#111827;font-family:Segoe UI,Arial,sans-serif}}
header{{position:sticky;top:0;z-index:5;background:rgba(255,255,255,.96);backdrop-filter:blur(12px);border-bottom:1px solid #e5e7eb;padding:18px 22px}}
.wrap{{max-width:1180px;margin:auto}} h1{{margin:0;font-size:24px}} .sub{{margin-top:5px;color:#6b7280;font-size:13px}}
.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:15px}}
.stat{{background:#111827;color:white;border-radius:14px;padding:12px 15px}} .stat b{{display:block;font-size:20px}} .stat small{{opacity:.7}}
.feedbox{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-top:12px}} .feedstatus{{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:11px 13px;font-size:12px;line-height:1.4}} .feedstatus b{{display:block;font-size:13px;margin-bottom:3px}} .tools{{display:flex;gap:10px;margin-top:14px}} input,select{{border:1px solid #d1d5db;border-radius:10px;padding:11px 12px;background:#fff;font-size:14px;min-width:180px}}
main{{max-width:1180px;margin:20px auto;padding:0 16px 50px}} .grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}}
.card{{background:white;border:1px solid #e5e7eb;border-radius:16px;padding:17px;box-shadow:0 3px 10px rgba(0,0,0,.03);transition:.18s}}
.card.done{{opacity:.42}} .card.done .copy{{background:#6b7280}} .top{{display:flex;justify-content:space-between;align-items:center}}
.time{{font-size:20px;font-weight:900}} .score{{background:#fff7ed;color:#c2410c;border-radius:999px;padding:7px 10px;font-weight:900}}
h2{{font-size:16px;line-height:1.35;margin:14px 0}} .meta{{display:flex;flex-wrap:wrap;gap:6px}}
.pill{{font-size:11px;background:#f3f4f6;border-radius:999px;padding:5px 8px}} .pill.good{{background:#dcfce7;color:#166534}}
.price{{display:flex;align-items:center;gap:10px;margin:17px 0}} .old{{text-decoration:line-through;color:#9ca3af}} .price strong{{font-size:25px}}
.discount{{background:#dcfce7;color:#166534;font-weight:800;border-radius:8px;padding:5px 7px}}
.actions{{display:grid;grid-template-columns:1.2fr 1fr 1fr;gap:8px}} button{{border:0;border-radius:10px;padding:11px 8px;font-weight:800;cursor:pointer;background:#eef2f7;color:#111827}}
button.copy{{background:#ee4d2d;color:white}} button:hover{{filter:brightness(.96)}} .toast{{position:fixed;right:20px;bottom:20px;background:#111827;color:white;border-radius:10px;padding:12px 16px;opacity:0;pointer-events:none;transition:.2s}}
.toast.show{{opacity:1}}
@media(max-width:760px){{.grid{{grid-template-columns:1fr}}.stats{{grid-template-columns:repeat(2,1fr)}}.feedbox{{grid-template-columns:1fr}}.actions{{grid-template-columns:1fr}}.tools{{flex-direction:column}}input,select{{width:100%}}}}
</style>
</head>
<body>
<header>
<div class="wrap">
<h1>🔥 Shopee Caçador V3</h1>
<div class="sub">Atualizado em {generated} • fila do dia seguinte • links afiliados prontos</div>
<div class="stats">
  <div class="stat"><b>{stats["total_rows"]:,}</b><small>linhas analisadas</small></div>
  <div class="stat"><b>{stats["unique_items"]:,}</b><small>produtos únicos</small></div>
  <div class="stat"><b>{stats["candidates"]:,}</b><small>candidatos</small></div>
  <div class="stat"><b>{len(selected)}</b><small>ofertas na fila</small></div>
</div>
<div class="feedbox">{feed_box_html}</div>
<div class="tools">
<input id="search" placeholder="Buscar produto..." oninput="filterCards()">
<select id="cat" onchange="filterCards()"><option value="">Todas categorias</option></select>
<button onclick="clearPosted()">Limpar marcados</button>
</div>
</div>
</header>
<main><div class="grid" id="grid">{body}</div></main>
<div class="toast" id="toast">Copiado!</div>
<script>
const cards=[...document.querySelectorAll('.card')];
const cat=document.getElementById('cat');
[...new Set(cards.map(c=>c.dataset.cat))].sort().forEach(c=>{{const o=document.createElement('option');o.value=c;o.textContent=c;cat.appendChild(o);}});
function copyPost(text){{navigator.clipboard.writeText(text).then(()=>toast('Post copiado!'));}}
function openProduct(url){{window.open(url,'_blank');}}
function key(id){{return 'shopee_v3_posted_'+id;}}
function togglePosted(id,btn){{const card=btn.closest('.card');if(localStorage.getItem(key(id))){{localStorage.removeItem(key(id));card.classList.remove('done');btn.textContent='✓ MARCAR POSTADO';}}else{{localStorage.setItem(key(id),'1');card.classList.add('done');btn.textContent='↩ DESMARCAR';}}}}
function restore(){{cards.forEach(c=>{{if(localStorage.getItem(key(c.dataset.id))){{c.classList.add('done');c.querySelector('.posted').textContent='↩ DESMARCAR';}}}});}}
function clearPosted(){{if(!confirm('Limpar todos os marcados?'))return;cards.forEach(c=>{{localStorage.removeItem(key(c.dataset.id));c.classList.remove('done');c.querySelector('.posted').textContent='✓ MARCAR POSTADO';}});}}
function filterCards(){{const q=document.getElementById('search').value.toLowerCase().trim();const category=cat.value;cards.forEach(c=>{{c.style.display=(!q||c.dataset.title.includes(q))&&(!category||c.dataset.cat===category)?'':'none';}});}}
function toast(t){{const e=document.getElementById('toast');e.textContent=t;e.classList.add('show');setTimeout(()=>e.classList.remove('show'),1400);}}
restore();
</script>
</body>
</html>"""
    path.write_text(doc, encoding="utf-8")

def main(open_panel=False, local_test_paths=None):
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = apply_env_overrides(json.load(f))

    affiliate_id = str(cfg["affiliate_id"])
    sub_id = str(cfg["sub_id"])
    filtros = cfg["filtros"]
    observed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    day_key = datetime.now().strftime("%Y%m%d")

    previous_feed_state = load_feed_state()
    next_feed_state = dict(previous_feed_state)
    feed_statuses = []

    if local_test_paths:
        downloaded = [Path(x) for x in local_test_paths]
        log("MODO TESTE LOCAL.")
        for i, target in enumerate(downloaded):
            name = cfg["feeds"][i]["nome"] if i < len(cfg["feeds"]) else target.name
            fs = evaluate_feed_freshness(name, target, previous_feed_state)
            feed_statuses.append(fs)
            next_feed_state[name] = fs
    else:
        downloaded = []
        log("Baixando feeds da Shopee...")
        for i, feed in enumerate(cfg["feeds"], 1):
            target = DOWNLOAD_DIR / f"feed_{i}_{day_key}.csv"
            try:
                download_feed(feed["url"], target)
                if target.stat().st_size < 1000:
                    raise RuntimeError("arquivo muito pequeno")
                downloaded.append(target)
                fs = evaluate_feed_freshness(feed["nome"], target, previous_feed_state)
                feed_statuses.append(fs)
                next_feed_state[feed["nome"]] = fs
                log(f"OK: {feed['nome']} ({target.stat().st_size/1024/1024:.1f} MB) | {freshness_label(fs['status'])}")
            except Exception as e:
                fs = {
                    "nome": feed["nome"],
                    "status": "ERRO_DOWNLOAD",
                    "hash": "",
                    "size_bytes": 0,
                    "last_success_at": "",
                    "last_changed_at": (previous_feed_state.get(feed["nome"]) or {}).get("last_changed_at", ""),
                    "age_hours": (previous_feed_state.get(feed["nome"]) or {}).get("age_hours", 0),
                    "erro": str(e),
                }
                feed_statuses.append(fs)
                log(f"ERRO em {feed['nome']}: {e}")

    if not downloaded:
        raise RuntimeError("Nenhum feed disponível.")

    save_feed_state(next_feed_state)

    items, total_rows = load_items(downloaded)
    log(f"{total_rows:,} linhas | {len(items):,} itens únicos")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    candidates = []
    now_dt = datetime.now()
    cutoff = now_dt - timedelta(days=int(filtros["dias_sem_repetir"]))

    for r in items.values():
        sale = fnum(r.get("sale_price"))
        normal = fnum(r.get("price"))
        disc = fnum(r.get("discount_percentage"))
        ir = fnum(r.get("item_rating"))
        sr = fnum(r.get("shop_rating"), filtros["nota_loja_minima"])

        if not (filtros["preco_minimo"] <= sale <= filtros["preco_maximo"]):
            continue
        if normal <= sale or disc < filtros["desconto_minimo"] or ir < filtros["nota_produto_minima"]:
            continue
        if r.get("shop_rating") not in ("", None) and sr < filtros["nota_loja_minima"]:
            continue

        itemid = str(r.get("itemid") or "")
        hist = historical_stats(conn, itemid, sale)

        served = conn.execute("SELECT served_at,price FROM served WHERE itemid=?", (itemid,)).fetchone()
        if served:
            try:
                served_at = datetime.fromisoformat(str(served[0]))
            except Exception:
                served_at = now_dt
            old_price = float(served[1])
            improvement = ((old_price - sale) / old_price * 100) if old_price else 0
            if served_at >= cutoff and improvement < filtros["queda_para_repetir_pct"]:
                continue

        r["_score"] = final_score(r, hist)
        r["_hist"] = hist
        candidates.append(r)

    candidates.sort(
        key=lambda r: (r["_score"], r["_hist"]["drop_vs_avg"], fnum(r.get("discount_percentage")), fint(r.get("like"))),
        reverse=True
    )

    slots = make_queue_times(cfg)
    selected_rows = []
    cat_counts = {}
    max_cat = int(filtros["max_por_categoria"])

    for r in candidates:
        cat = str(r.get("global_category1") or "Outros")
        if cat_counts.get(cat, 0) >= max_cat:
            continue
        selected_rows.append(r)
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
        if len(selected_rows) >= len(slots):
            break

    selected = []
    for slot, r in zip(slots, selected_rows):
        base = r.get("product_short link") or r.get("product_link") or ""
        link = make_affiliate_link(base, affiliate_id, sub_id)
        item = {
            "slot": slot, "row": r, "hist": r["_hist"], "score": r["_score"],
            "link": link, "observed_at": observed_at
        }
        item["copy"] = make_copy(r, link, item["score"], item["hist"])
        selected.append(item)

    for r in candidates[:5000]:
        conn.execute(
            "INSERT OR IGNORE INTO snapshots(itemid,observed_at,sale_price,original_price,discount) VALUES(?,?,?,?,?)",
            (str(r.get("itemid") or ""), observed_at, fnum(r.get("sale_price")), fnum(r.get("price")), fnum(r.get("discount_percentage")))
        )

    for x in selected:
        r = x["row"]
        conn.execute(
            """
            INSERT INTO served(itemid,served_at,price)
            VALUES(?,?,?)
            ON CONFLICT(itemid) DO UPDATE SET served_at=excluded.served_at, price=excluded.price
            """,
            (str(r.get("itemid") or ""), datetime.now().isoformat(timespec="seconds"), fnum(r.get("sale_price")))
        )

    conn.commit()
    conn.close()

    csv_path = OUTPUT_DIR / f"ofertas_{day_key}.csv"
    html_path = OUTPUT_DIR / "PAINEL.html"
    latest_json_path = OUTPUT_DIR / "latest.json"
    run_stats = {"total_rows": total_rows, "unique_items": len(items), "candidates": len(candidates)}
    write_csv(selected, csv_path)
    write_html(selected, html_path, run_stats, feed_statuses)
    write_latest_json(selected, latest_json_path, run_stats, feed_statuses)

    feed_lines = []
    for fs in feed_statuses:
        feed_lines.append(
            f"{fs.get('nome')}: {freshness_label(fs.get('status'))} | "
            f"última mudança: {fs.get('last_changed_at','-')} | "
            f"sem mudança há {fs.get('age_hours',0)}h"
        )

    (OUTPUT_DIR / "ULTIMA_EXECUCAO.txt").write_text(
        f"Execução: {observed_at}\n"
        f"Linhas: {total_rows}\n"
        f"Itens únicos: {len(items)}\n"
        f"Candidatos: {len(candidates)}\n"
        f"Fila: {len(selected)}\n\n"
        + "\n".join(feed_lines) + "\n",
        encoding="utf-8"
    )

    log(f"{len(candidates):,} candidatos")
    log(f"{len(selected)} ofertas na fila")
    log(f"Painel: {html_path}")

    if open_panel:
        try:
            webbrowser.open(html_path.as_uri())
        except Exception:
            pass

    return html_path

if __name__ == "__main__":
    import traceback
    try:
        main(open_panel="--abrir" in sys.argv)
        print()
        print("FINALIZADO COM SUCESSO.")
    except Exception:
        print()
        print("=" * 72)
        print("ERRO NO SHOPEE CACADOR V3")
        print("=" * 72)
        traceback.print_exc()
        print("=" * 72)
        raise SystemExit(1)
