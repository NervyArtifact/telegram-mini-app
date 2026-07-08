"""
Бесплатный вариант напоминалки — рассчитан на запуск по расписанию
через GitHub Actions (без постоянно работающего сервера).

Каждый запуск:
  1. Забирает новые сообщения от бота (getUpdates) и обрабатывает команды.
  2. Проверяет все активные задачи — если пора напомнить, шлёт сообщение.
  3. Сохраняет состояние в tasks.json (коммитит workflow).

Команды в боте:
  /add <текст задачи> <интервал>   — например: /add Позвонить маме 2h
  /list                            — активные задачи
  /done <id>                       — отметить выполненной
  /start, /help                    — помощь

Ответы и напоминания приходят не мгновенно, а при следующем запуске
workflow (по умолчанию — раз в 15 минут).
"""

import json
import os
import re
import time
import urllib.request
import urllib.parse

STATE_PATH = os.environ.get("STATE_PATH", "tasks.json")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

INTERVAL_RE = re.compile(r"^(\d+)\s*([mhd])$", re.IGNORECASE)
UNIT_TO_MINUTES = {"m": 1, "h": 60, "d": 60 * 24}

HELP_TEXT = (
    "Привет! Я напоминаю про задачи, пока не отметишь их выполненными.\n\n"
    "Добавить задачу:\n/add Позвонить маме 2h\n"
    "Форматы интервала: 30m (минуты), 2h (часы), 1d (дни)\n\n"
    "Другие команды: /list, /done <id>\n\n"
    "⚠️ Я проверяю сообщения не мгновенно, а раз в ~15 минут — "
    "это бесплатная версия без постоянного сервера."
)


# ---------- Состояние ----------

def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"next_id": 1, "update_offset": 0, "tasks": []}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------- Telegram API (без сторонних библиотек) ----------

def api_call(method: str, params: dict):
    url = f"{API_BASE}/{method}"
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_updates(offset: int):
    result = api_call("getUpdates", {"offset": offset, "timeout": 0})
    return result.get("result", [])


def send_message(chat_id: int, text: str, with_done_button_for: int | None = None):
    params = {"chat_id": chat_id, "text": text}
    if with_done_button_for is not None:
        keyboard = {
            "inline_keyboard": [[{"text": "✅ Готово", "callback_data": f"done:{with_done_button_for}"}]]
        }
        params["reply_markup"] = json.dumps(keyboard)
    api_call("sendMessage", params)


def edit_message_text(chat_id: int, message_id: int, text: str):
    api_call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text})


def answer_callback_query(callback_query_id: str):
    api_call("answerCallbackQuery", {"callback_query_id": callback_query_id})


# ---------- Логика задач ----------

def parse_interval(token: str):
    m = INTERVAL_RE.match(token.strip())
    if not m:
        return None
    value, unit = m.groups()
    return int(value) * UNIT_TO_MINUTES[unit.lower()]


def human_interval(minutes: int) -> str:
    if minutes % (60 * 24) == 0:
        return f"{minutes // (60 * 24)}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def find_task(state, task_id: int):
    for t in state["tasks"]:
        if t["id"] == task_id:
            return t
    return None


def handle_command(state, chat_id: int, text: str):
    text = text.strip()

    if text in ("/start", "/help"):
        send_message(chat_id, HELP_TEXT)
        return

    if text.startswith("/add"):
        args = text[len("/add"):].strip().split()
        if len(args) < 2:
            send_message(chat_id, "Формат: /add <текст задачи> <интервал>\nПример: /add Позвонить маме 2h")
            return
        *text_parts, interval_token = args
        interval_minutes = parse_interval(interval_token)
        if interval_minutes is None:
            send_message(chat_id, "Не понял интервал. Формат: 30m / 2h / 1d")
            return
        task_text = " ".join(text_parts).strip()
        if not task_text:
            send_message(chat_id, "Текст задачи не может быть пустым.")
            return

        task_id = state["next_id"]
        state["next_id"] += 1
        state["tasks"].append({
            "id": task_id,
            "chat_id": chat_id,
            "text": task_text,
            "interval_minutes": interval_minutes,
            "done": False,
            "last_sent": None,
        })
        send_message(chat_id, f"Добавил задачу #{task_id}: «{task_text}»\nБуду напоминать каждые {interval_token}.")
        return

    if text == "/list":
        active = [t for t in state["tasks"] if t["chat_id"] == chat_id and not t["done"]]
        if not active:
            send_message(chat_id, "Активных задач нет 🎉")
            return
        lines = ["Активные задачи:"]
        for t in active:
            lines.append(f"#{t['id']} — {t['text']} (каждые {human_interval(t['interval_minutes'])})")
        send_message(chat_id, "\n".join(lines))
        return

    if text.startswith("/done"):
        args = text[len("/done"):].strip().split()
        if not args:
            send_message(chat_id, "Формат: /done <id> (номер из /list)")
            return
        try:
            task_id = int(args[0])
        except ValueError:
            send_message(chat_id, "id должен быть числом.")
            return
        task = find_task(state, task_id)
        if task and not task["done"]:
            task["done"] = True
            send_message(chat_id, f"Задача #{task_id} отмечена выполненной ✅")
        else:
            send_message(chat_id, "Такой активной задачи не нашёл.")
        return


def process_updates(state):
    updates = get_updates(state["update_offset"])
    for update in updates:
        state["update_offset"] = update["update_id"] + 1

        if "message" in update and "text" in update["message"]:
            chat_id = update["message"]["chat"]["id"]
            handle_command(state, chat_id, update["message"]["text"])

        elif "callback_query" in update:
            cq = update["callback_query"]
            data = cq.get("data", "")
            if data.startswith("done:"):
                task_id = int(data.split(":")[1])
                task = find_task(state, task_id)
                chat_id = cq["message"]["chat"]["id"]
                message_id = cq["message"]["message_id"]
                if task and not task["done"]:
                    task["done"] = True
                    edit_message_text(chat_id, message_id, f"{cq['message']['text']}\n\n✅ Выполнено")
                answer_callback_query(cq["id"])


def send_due_reminders(state):
    now = int(time.time())
    for t in state["tasks"]:
        if t["done"]:
            continue
        interval_seconds = t["interval_minutes"] * 60
        last_sent = t["last_sent"] or 0
        if now - last_sent >= interval_seconds:
            send_message(t["chat_id"], f"⏰ Напоминание: {t['text']}", with_done_button_for=t["id"])
            t["last_sent"] = now


def main():
    if not BOT_TOKEN:
        raise SystemExit("Задайте переменную окружения BOT_TOKEN (секрет репозитория).")

    state = load_state()
    process_updates(state)
    send_due_reminders(state)
    save_state(state)


if __name__ == "__main__":
    main()
