"""Профили поставщиков с показанным финальным действием; черновик не равен отправке."""
import re
from urllib.parse import urlsplit

PROFILES = {
    "favorit-parts.ru": {"name": "Фаворит", "final": ("Вернуть",), "reason": ("отказ",),
                        "success": r"(?:заявка|возврат).{0,80}(?:успешно создан|оформлен|отправлен)"},
    "berg.ru": {"name": "Берг", "final": ("Создать заявку",), "reason": ("посогласованиюсторон", "отказклиента"),
                "success": r"Заявка на возврат успешно создана"},
    "autokontinent.ru": {"name": "Автоконтинент", "final": ("Отправить",), "reason": ("качествен", "отказклиента"),
                        "success": r"Ваша заявка на возврат отправлена"},
    "moskvorechie.ru": {"name": "Москворечье", "final": ("Оформить возврат",), "reason": ("отказклиента",),
                        "success": r"Возвратная накладная была успешно создана"},
    "part-kom.ru": {"name": "Партком", "final": ("Отправить запрос",), "reason": ("детальненужна", "отказклиента"),
                    "success": r"(?:запрос|заявка).{0,60}успешно (?:отправлен|создан|зарегистрирован)"},
    "motexc.ru": {"name": "Мотех", "final": ("Вернуть",), "reason": ("возврат", "отказклиента"),
                 "success": r"(?:запрос|заявка|возврат).{0,60}успешно (?:отправлен|создан|оформлен|зарегистрирован)"},
    "ixora-auto.ru": {"name": "Иксора", "final": ("Отправить запрос",), "reason": ("отказотдетали",),
                     "success": r"(?:запрос|заявка).{0,60}успешно (?:отправлен|создан|зарегистрирован)"},
}

# В этих двух видео финальное оформление и обязательные реквизиты не подтверждены.
UNVERIFIED = {"rossko.ru": "В инструкции показано сохранение черновика, финальное оформление не показано",
              "shate-m.ru": "Не проверены финальная отправка и источник обязательных реквизитов возвратного документа"}


def profile_for(url):
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        return None
    return next((value for domain, value in PROFILES.items() if host == domain or host.endswith("." + domain)), None)


def confirmed_message(profile, text):
    match = re.search(profile["success"], text, re.I)
    return match.group(0) if match else ""
