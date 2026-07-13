import os
import re
import sys
import json
import html
import time
import requests
from bs4 import BeautifulSoup

URL = "https://uainvest.com.ua/launchpools"   # активні лаунчпули
BASE = "https://uainvest.com.ua"
STATE_FILE = "seen_gate_launchpools.json"     # файл пам'яті (номери вже надісланих пулів)

# Скільки разів пробувати мережеві операції перед тим, як здатися
MAX_FETCH_ATTEMPTS = 3
MAX_SEND_ATTEMPTS = 4
# Пауза між надсиланнями, щоб не впертись у ліміт Telegram (секунди)
SEND_INTERVAL = 1.0

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
    """Завантажує сторінку з кількома спробами.

    Якщо сайт стабільно недоступний — тихо виходимо з кодом 0 (без падіння
    workflow), щоб просто спробувати знову за розкладом.
    """
    last_err = None
    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        try:
            r = requests.get(URL, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            last_err = e
            print(f"Сайт недоступний (спроба {attempt}/{MAX_FETCH_ATTEMPTS}): {e}")
            if attempt < MAX_FETCH_ATTEMPTS:
                time.sleep(min(2 ** attempt, 20))
    print(f"Сайт недоступний цього разу, пропускаю запуск: {last_err}")
    sys.exit(0)


def parse_gate_launchpools(page_html):
    """Повертає {id: {...}} тільки для рядків біржі Gate.

    Кожен рядок обробляється ізольовано: помилка в одному рядку не ламає
    розбір усієї сторінки.
    """
    soup = BeautifulSoup(page_html, "html.parser")
    results = {}
    for row in soup.find_all("tr"):
        try:
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
        except Exception as e:  # noqa: BLE001 — один поганий рядок не має валити весь розбір
            print(f"Пропущено рядок через помилку розбору: {e}")
            continue
    return results


def load_seen():
    """Повертає множину вже надісланих ID.

    None -> перший запуск АБО пошкоджений/порожній файл: у такому разі робимо
    ре-базлайн (запам'ятовуємо поточний стан без сповіщень), щоб не спамити.
    """
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("очікував список ID, отримав інше")
        return {str(x) for x in data}
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"Файл пам'яті пошкоджений або порожній ({e}). Роблю ре-базлайн без сповіщень.")
        return None


def save_seen(ids):
    """Атомарний запис файлу пам'яті (через тимчасовий файл + rename)."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(ids, key=int), f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def send_telegram(info):
    """Надсилає одне повідомлення в Telegram.

    Повертає True при успіху, False — якщо надіслати не вдалося. Робить кілька
    спроб при тимчасових помилках і поважає ліміт (429 з retry_after).
    """
    text = (
        "🚀 <b>Новий лаунчпул Gate!</b>\n\n"
        f"🪙 Токен: <b>{html.escape(info['token'])}</b>\n"
        f"💰 Нагорода: {html.escape(info['reward'] or '—')}\n"
        f"📅 Період: {html.escape(info['period'] or '—')}\n"
        f"📊 Статус: {html.escape(info['status'] or '—')}\n\n"
        f"🔗 {html.escape(info['link'] or '')}"
    )
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}

    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            resp = requests.post(api, data=payload, timeout=30)
        except requests.RequestException as e:
            print(f"  Помилка мережі при надсиланні (спроба {attempt}/{MAX_SEND_ATTEMPTS}): {e}")
            time.sleep(min(2 ** attempt, 30))
            continue

        if resp.status_code == 200:
            return True

        # Ліміт швидкості — чекаємо стільки, скільки просить Telegram, і повторюємо.
        if resp.status_code == 429:
            retry_after = 5
            try:
                retry_after = int(resp.json().get("parameters", {}).get("retry_after", retry_after))
            except (ValueError, json.JSONDecodeError):
                pass
            print(f"  Ліміт Telegram (429). Чекаю {retry_after}s і повторюю.")
            time.sleep(retry_after + 1)
            continue

        # Тимчасові помилки сервера — повторюємо з backoff.
        if 500 <= resp.status_code < 600:
            print(f"  Сервер Telegram {resp.status_code} (спроба {attempt}/{MAX_SEND_ATTEMPTS}). Повтор.")
            time.sleep(min(2 ** attempt, 30))
            continue

        # Інші помилки (напр. 400) повторювати марно — фіксуємо й виходимо без успіху.
        print(f"  Telegram відхилив повідомлення ({resp.status_code}): {resp.text[:300]}")
        return False

    print("  Не вдалося надіслати після всіх спроб.")
    return False


def main():
    current = parse_gate_launchpools(fetch_html())
    current_ids = set(current.keys())
    print(f"Знайдено пулів Gate зараз: {len(current_ids)} -> {[current[i]['token'] for i in current_ids]}")

    seen = load_seen()

    # Перший запуск (або пошкоджений файл): просто запам'ятовуємо стан, БЕЗ сповіщень.
    if seen is None:
        save_seen(current_ids)
        print(f"Ре-базлайн. Записав {len(current_ids)} пул(ів). Сповіщення не надсилав.")
        return

    new_ids = current_ids - seen
    if not new_ids:
        print("Нових пулів Gate немає.")
        return

    # Надсилаємо по черзі; у пам'ять додаємо ЛИШЕ успішно надіслані, тож у разі
    # збою повідомлення не дублюються, а невдалі спробуємо наступного запуску.
    sent = set()
    ordered = sorted(new_ids, key=int)
    for i, pid in enumerate(ordered):
        if send_telegram(current[pid]):
            sent.add(pid)
            print(f"Надіслано сповіщення: пул {pid} ({current[pid]['token']})")
        else:
            print(f"НЕ надіслано (повторю наступного разу): пул {pid} ({current[pid]['token']})")
        if i < len(ordered) - 1:
            time.sleep(SEND_INTERVAL)

    if sent:
        save_seen(seen | sent)
        print(f"Оновлено пам'ять: +{len(sent)} пул(ів).")
    else:
        print("Жодного повідомлення не надіслано — пам'ять без змін.")


if __name__ == "__main__":
    main()
