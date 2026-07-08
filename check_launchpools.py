import os
import re
import sys
import json
import html
import requests
from bs4 import BeautifulSoup

URL = "https://uainvest.com.ua/launchpools"   # активні лаунчпули
BASE = "https://uainvest.com.ua"
STATE_FILE = "seen_gate_launchpools.json"     # файл пам'яті (номери вже надісланих пулів)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
if not BOT_TOKEN or not CHAT_ID:
    sys.exit("Немає секретів TELEGRAM_BOT_TOKEN або TELEGRAM_CHAT_ID.")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "uk,en;q=0.9",
}


def fetch_html():
    """Завантажує сторінку. Якщо сайт тимчасово недоступний — тихо виходимо (без падіння)."""
    try:
        r = requests.get(URL, headers=HEADERS, timeout=30)
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:
        print(f"Сайт недоступний цього разу: {e}")
        sys.exit(0)


def parse_gate_launchpools(page_html):
    """Повертає {id: {...}} тільки для рядків біржі Gate."""
    soup = BeautifulSoup(page_html, "html.parser")
    results = {}
    for row in soup.find_all("tr"):
        # 1) унікальний номер пулу + посилання "Детальніше"
        pool_id, link = None, None
        for a in row.find_all("a", href=True):
            m = re.search(r"/launchpools/(\d+)", a["href"])
            if m:
                pool_id = m.group(1)
                href = a["href"]
                link = href if href.startswith("http") else BASE + href
                break
        if not pool_id:
            continue

        # 2) чи це рядок Gate? Спершу за іконкою біржі, потім — за посиланням на gate.io
        is_gate = any(
            "/images/platforms/gate" in (img.get("src") or "").lower()
            for img in row.find_all("img")
        )
        if not is_gate:
            is_gate = any("gate.io" in a["href"].lower() for a in row.find_all("a", href=True))
        if not is_gate:
            continue

        # 3) поля для повідомлення
        cells = row.find_all("td")
        texts = [c.get_text(" ", strip=True) for c in cells]
        token = "?"
        for c in cells:
            img = c.find("img")
            if img and "/images/assets/" in (img.get("src") or ""):
                token = c.get_text(" ", strip=True) or token
                break
        reward = texts[2] if len(texts) > 2 else ""
        period = texts[4] if len(texts) > 4 else ""
        status = texts[5] if len(texts) > 5 else ""

        results[pool_id] = {
            "id": pool_id, "token": token, "reward": reward,
            "period": period, "status": status, "link": link,
        }
    return results


def load_seen():
    """None -> перший запуск (файлу ще немає)."""
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except (json.JSONDecodeError, ValueError):
        return set()


def save_seen(ids):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids, key=lambda x: int(x)), f, ensure_ascii=False, indent=2)


def send_telegram(info):
    text = (
        "🚀 <b>Новий лаунчпул Gate!</b>\n\n"
        f"🪙 Токен: <b>{html.escape(info['token'])}</b>\n"
        f"💰 Нагорода: {html.escape(info['reward'] or '—')}\n"
        f"📅 Період: {html.escape(info['period'] or '—')}\n"
        f"📊 Статус: {html.escape(info['status'] or '—')}\n\n"
        f"🔗 {info['link']}"
    )
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(
        api,
        data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=30,
    )
    resp.raise_for_status()


def main():
    current = parse_gate_launchpools(fetch_html())
    current_ids = set(current.keys())
    print(f"Знайдено пулів Gate зараз: {len(current_ids)} -> {[current[i]['token'] for i in current_ids]}")

    seen = load_seen()

    # Перший запуск: просто запам'ятовуємо те, що вже є, БЕЗ сповіщень.
    if seen is None:
        save_seen(current_ids)
        print(f"Перший запуск. Записав {len(current_ids)} пул(ів). Сповіщення не надсилав.")
        return

    new_ids = current_ids - seen
    if not new_ids:
        print("Нових пулів Gate немає.")
        return

    for pid in sorted(new_ids, key=lambda x: int(x)):
        send_telegram(current[pid])
        print(f"Надіслано сповіщення: пул {pid} ({current[pid]['token']})")

    save_seen(seen | current_ids)


if __name__ == "__main__":
    main()
