"""
Telegram-алерты для GitHub Actions.

Использование:
    from telegram_alert import send_alert
    send_alert("⚠️ Jetour: парсер не запустился сегодня")

Переменные окружения (берутся из GitHub Secrets):
    TELEGRAM_BOT_TOKEN — токен бота от @BotFather
    TELEGRAM_CHAT_ID   — ID чата/пользователя, куда слать
"""

import os
import sys
import requests


def send_alert(message: str) -> bool:
    """
    Отправляет сообщение в Telegram. Возвращает True если успешно.
    Не падает при ошибке — просто пишет в stderr и возвращает False.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("[telegram] BOT_TOKEN или CHAT_ID не заданы — алерт не отправлен",
              file=sys.stderr)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"[telegram] Ошибка отправки: {e}", file=sys.stderr)
        return False


if __name__ == "__main__":
    # Ручной тест: python telegram_alert.py "test message"
    msg = sys.argv[1] if len(sys.argv) > 1 else "✅ Test message from GitHub Actions"
    ok = send_alert(msg)
    sys.exit(0 if ok else 1)
