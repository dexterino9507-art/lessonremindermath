import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events
import requests

TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Tashkent"))
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TUTOR_CHAT_ID = os.environ.get("TUTOR_CHAT_ID", "").strip()
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
STUDENTS_DB = os.environ.get("NOTION_STUDENTS_DB", "674a209d0e39410aa8a7c7c893719aec")
REMINDERS_DB = os.environ.get("NOTION_REMINDERS_DB", "41a7c495e86681ccac1ee9e603b2fca0")
ICS_URL = os.environ.get("GCAL_ICS_URL", "")
POLICY_TEXT = os.environ.get("POLICY_TEXT", "").strip()

FIRST_REMINDER = timedelta(hours=24)
SECOND_REMINDER = timedelta(hours=2)
TUTOR_ALERT = timedelta(hours=3)

STATUS_WAITING = "Ожидает ответа"
STATUS_CONFIRMED = "Подтверждён"
STATUS_MOVE = "Перенос"

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
LOOKALIKES = str.maketrans("асеорхукмтнв", "aceopxykmthb")


# Telegram

def tg(method, **params):
    response = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=params, timeout=30)
    data = response.json()
    if not data.get("ok"):
        print(f"Telegram {method}: {data.get('description')}")
        return None
    return data["result"]


def send(chat_id, text, buttons=None):
    params = {"chat_id": chat_id, "text": text}
    if buttons:
        params["reply_markup"] = {"inline_keyboard": buttons}
    return tg("sendMessage", **params)


def send_long(chat_id, lines):
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) > 3800:
            send(chat_id, chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        send(chat_id, chunk)


# Notion

def notion(method, path, payload=None):
    response = requests.request(
        method,
        f"https://api.notion.com/v1/{path}",
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Notion {method} {path}: {response.status_code} {response.text}")
    return response.json()


def query_db(db_id, filter_=None):
    pages, cursor = [], None
    while True:
        payload = {"page_size": 100}
        if filter_:
            payload["filter"] = filter_
        if cursor:
            payload["start_cursor"] = cursor
        data = notion("POST", f"databases/{db_id}/query", payload)
        pages.extend(data["results"])
        if not data.get("has_more"):
            return pages
        cursor = data["next_cursor"]


def plain(prop):
    if not prop:
        return ""
    items = prop.get("title") or prop.get("rich_text") or []
    return "".join(item["plain_text"] for item in items).strip()


def text_value(value):
    return {"rich_text": [{"text": {"content": value}}]}


def update_page(page_id, properties):
    notion("PATCH", f"pages/{page_id}", {"properties": properties})


# Ученики

def normalize(title):
    title = title.lower().replace("ё", "е").translate(LOOKALIKES)
    return re.sub(r"\s+", " ", title).strip()


def load_students():
    students = []
    for page in query_db(STUDENTS_DB, {"property": "Активен", "checkbox": {"equals": True}}):
        props = page["properties"]
        aliases = [normalize(a) for a in plain(props.get("Из расписания")).split("/") if a.strip()]
        students.append({
            "id": page["id"].replace("-", ""),
            "name": plain(props.get("Имя")),
            "aliases": aliases,
            "chat_student": plain(props.get("Chat ID ученика")),
            "chat_parent": plain(props.get("Chat ID родителя")),
        })
    return students


def match_students(title, students):
    key = normalize(title)
    exact = [s for s in students if key in s["aliases"]]
    if exact:
        return exact
    best, best_len = [], 0
    for s in students:
        for alias in s["aliases"]:
            if key.startswith(alias + " ") and len(alias) >= best_len:
                if len(alias) > best_len:
                    best, best_len = [], len(alias)
                best.append(s)
    return best


# Календарь

def load_events(start, end):
    raw = requests.get(ICS_URL, timeout=60).content
    calendar = icalendar.Calendar.from_ical(raw)
    events = []
    for event in recurring_ical_events.of(calendar).between(start, end):
        if str(event.get("STATUS", "")).upper() == "CANCELLED":
            continue
        begin = event["DTSTART"].dt
        if not isinstance(begin, datetime):
            continue
        if begin.tzinfo is None:
            begin = begin.replace(tzinfo=TZ)
        events.append({
            "uid": str(event.get("UID", "")),
            "title": str(event.get("SUMMARY", "")).strip(),
            "start": begin.astimezone(TZ),
        })
    events.sort(key=lambda e: e["start"])
    return events


def describe_time(start, now):
    days = (start.date() - now.date()).days
    clock = start.strftime("%H:%M")
    if days == 0:
        return f"Сегодня в {clock}"
    if days == 1:
        return f"Завтра, {start.day} {MONTHS[start.month - 1]}, в {clock}"
    weekday = WEEKDAYS[start.weekday()].capitalize()
    return f"{weekday}, {start.day} {MONTHS[start.month - 1]}, в {clock}"


# Напоминания

def load_reminders(now):
    pages = query_db(REMINDERS_DB, {
        "property": "Начало",
        "date": {"on_or_after": (now - timedelta(days=1)).isoformat()},
    })
    reminders = {}
    for page in pages:
        props = page["properties"]
        status = props["Статус"]["select"]
        reminders[plain(props["Ключ"])] = {
            "id": page["id"].replace("-", ""),
            "status": status["name"] if status else STATUS_WAITING,
            "sent24": props["За 24 ч"]["checkbox"],
            "sent2": props["За 2 ч"]["checkbox"],
            "alerted": props["Сигнал преподавателю"]["checkbox"],
        }
    return reminders


def create_reminder(key, event, student):
    page = notion("POST", "pages", {
        "parent": {"database_id": REMINDERS_DB},
        "properties": {
            "Урок": {"title": [{"text": {"content": f"{student['name']} — {event['title']}"}}]},
            "Ученик": {"relation": [{"id": student["id"]}]},
            "Начало": {"date": {"start": event["start"].isoformat()}},
            "Статус": {"select": {"name": STATUS_WAITING}},
            "Ключ": text_value(key),
        },
    })
    return {"id": page["id"].replace("-", ""), "status": STATUS_WAITING,
            "sent24": False, "sent2": False, "alerted": False}


def recipients(student):
    result = []
    if student["chat_student"]:
        result.append((student["chat_student"], False))
    if student["chat_parent"]:
        result.append((student["chat_parent"], True))
    return result


def answer_buttons(reminder_id):
    return [[
        {"text": "Подтверждаю", "callback_data": f"ok:{reminder_id}"},
        {"text": "Нужен перенос", "callback_data": f"mv:{reminder_id}"},
    ]]


def first_text(student, when, for_parent):
    head = f"Напоминание об уроке ({student['name']})." if for_parent else "Напоминание об уроке."
    text = f"{head}\n{when}.\n\nПодтвердите, пожалуйста, участие."
    if POLICY_TEXT:
        text += f"\n\n{POLICY_TEXT}"
    return text


def second_text(student, when, for_parent, confirmed):
    subject = f"Урок ({student['name']})" if for_parent else "Урок"
    text = f"{subject} начнётся {when[0].lower()}{when[1:]}."
    if not confirmed:
        text += "\n\nОтвет на предыдущее напоминание не получен. Подтвердите, пожалуйста, участие."
    return text


def process_lessons(now, students):
    events = load_events(now - timedelta(minutes=5), now + FIRST_REMINDER + timedelta(minutes=5))
    reminders = load_reminders(now)

    for event in events:
        delta = event["start"] - now
        if delta <= timedelta(0) or delta > FIRST_REMINDER:
            continue
        for student in match_students(event["title"], students):
            chats = recipients(student)
            if not chats:
                continue
            key = f"{event['uid']}|{event['start'].isoformat()}|{student['id']}"
            reminder = reminders.get(key)
            existed = reminder is not None
            if reminder is None:
                reminder = create_reminder(key, event, student)
                reminders[key] = reminder

            when = describe_time(event["start"], now)
            confirmed = reminder["status"] == STATUS_CONFIRMED

            if reminder["status"] != STATUS_MOVE:
                if delta <= SECOND_REMINDER and not reminder["sent2"]:
                    for chat_id, for_parent in chats:
                        send(chat_id, second_text(student, when, for_parent, confirmed),
                             None if confirmed else answer_buttons(reminder["id"]))
                    update_page(reminder["id"], {"За 24 ч": {"checkbox": True}, "За 2 ч": {"checkbox": True}})
                    reminder["sent24"] = reminder["sent2"] = True
                elif delta > SECOND_REMINDER and not reminder["sent24"]:
                    for chat_id, for_parent in chats:
                        send(chat_id, first_text(student, when, for_parent), answer_buttons(reminder["id"]))
                    update_page(reminder["id"], {"За 24 ч": {"checkbox": True}})
                    reminder["sent24"] = True

            if (TUTOR_CHAT_ID and existed and reminder["status"] == STATUS_WAITING
                    and delta <= TUTOR_ALERT and not reminder["alerted"]):
                send(TUTOR_CHAT_ID, f"Нет подтверждения урока: {student['name']}.\n{when}.")
                update_page(reminder["id"], {"Сигнал преподавателю": {"checkbox": True}})
                reminder["alerted"] = True


# Входящие сообщения

def bot_username():
    me = tg("getMe")
    return me["username"] if me else ""


def handle_start(message, payload, students_by_id):
    chat_id = message["chat"]["id"]
    match = re.fullmatch(r"([sp])_([0-9a-f]{32})", payload or "")
    if not match:
        send(chat_id, f"Ваш идентификатор: {chat_id}\n\n"
                      "Для подключения к напоминаниям воспользуйтесь ссылкой, полученной от преподавателя.")
        return
    role, page_id = match.groups()
    student = students_by_id.get(page_id)
    if not student:
        send(chat_id, "Ссылка недействительна. Обратитесь к преподавателю.")
        return
    field = "Chat ID ученика" if role == "s" else "Chat ID родителя"
    update_page(page_id, {field: text_value(str(chat_id))})
    student["chat_student" if role == "s" else "chat_parent"] = str(chat_id)
    send(chat_id, f"Подключение выполнено. Напоминания об уроках ({student['name']}) "
                  "будут приходить в этот чат за сутки и за два часа до начала.")


def handle_links(chat_id, students):
    username = bot_username()
    lines = ["Ссылки для подключения (ученик / родитель):", ""]
    for s in sorted(students, key=lambda x: x["name"]):
        marks = ("ученик подключён" if s["chat_student"] else "ученик не подключён") + ", " + \
                ("родитель подключён" if s["chat_parent"] else "родитель не подключён")
        lines += [f"{s['name']} ({marks})",
                  f"https://t.me/{username}?start=s_{s['id']}",
                  f"https://t.me/{username}?start=p_{s['id']}", ""]
    send_long(chat_id, lines)


def handle_check(chat_id, now, students):
    events = load_events(now, now + timedelta(days=7))
    unmatched = sorted({e["title"] for e in events if not match_students(e["title"], students)})
    lines = ["События календаря на 7 дней без ученика в Notion:"]
    lines += [f"— {t}" for t in unmatched] or ["нет"]
    lines += ["", "Ученики без подключённого Telegram:"]
    lines += [f"— {s['name']}" for s in students if not recipients(s)] or ["нет"]
    send_long(chat_id, lines)


def handle_callback(query, now):
    action, _, reminder_id = query.get("data", "").partition(":")
    if action not in ("ok", "mv") or not re.fullmatch(r"[0-9a-f]{32}", reminder_id):
        return
    page = notion("GET", f"pages/{reminder_id}")
    props = page["properties"]
    title = plain(props["Урок"])
    start = datetime.fromisoformat(props["Начало"]["date"]["start"]).astimezone(TZ)
    when = describe_time(start, now)

    message = query.get("message")
    if action == "ok":
        update_page(reminder_id, {"Статус": {"select": {"name": STATUS_CONFIRMED}}})
        note, answer = "Участие подтверждено.", "Спасибо, участие подтверждено."
    else:
        update_page(reminder_id, {"Статус": {"select": {"name": STATUS_MOVE}}})
        note, answer = "Запрос на перенос передан преподавателю.", "Запрос на перенос передан преподавателю."
        if TUTOR_CHAT_ID:
            sender = query["from"]
            who = sender.get("first_name", "")
            if sender.get("username"):
                who += f" (@{sender['username']})"
            send(TUTOR_CHAT_ID, f"Запрос на перенос урока: {title}.\n{when}.\nОтправитель: {who}")

    tg("answerCallbackQuery", callback_query_id=query["id"], text=answer)
    if message:
        tg("editMessageText", chat_id=message["chat"]["id"], message_id=message["message_id"],
           text=f"{message.get('text', '')}\n\n{note}")


def process_updates(now, students):
    updates = tg("getUpdates", timeout=0, allowed_updates=["message", "callback_query"]) or []
    students_by_id = {s["id"]: s for s in students}
    for update in updates:
        try:
            if "callback_query" in update:
                handle_callback(update["callback_query"], now)
                continue
            message = update.get("message")
            if not message or "text" not in message:
                continue
            chat_id = message["chat"]["id"]
            command, _, payload = message["text"].strip().partition(" ")
            command = command.split("@")[0]
            is_tutor = str(chat_id) == TUTOR_CHAT_ID
            if command == "/start":
                handle_start(message, payload.strip(), students_by_id)
            elif command == "/links" and is_tutor:
                handle_links(chat_id, students)
            elif command == "/check" and is_tutor:
                handle_check(chat_id, now, students)
            elif not is_tutor:
                send(chat_id, "Бот отправляет напоминания об уроках. По остальным вопросам обращайтесь к преподавателю.")
        except Exception as error:
            print(f"Ошибка обработки обновления {update.get('update_id')}: {error}")
    if updates:
        tg("getUpdates", offset=updates[-1]["update_id"] + 1, limit=1, timeout=0)


def main():
    missing = [name for name, value in [("TELEGRAM_BOT_TOKEN", BOT_TOKEN), ("NOTION_TOKEN", NOTION_TOKEN),
                                        ("GCAL_ICS_URL", ICS_URL)] if not value]
    if missing:
        raise SystemExit("Не заданы переменные: " + ", ".join(missing))
    now = datetime.now(TZ)
    students = load_students()
    process_updates(now, students)
    process_lessons(now, students)


if __name__ == "__main__":
    main()
