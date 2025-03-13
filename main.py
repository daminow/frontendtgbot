#!/usr/bin/env python3
import os
import logging
import re
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# Настройка логирования с выводом в консоль
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Получение токена (лучше хранить его в переменной окружения)
TOKEN = "7501357038:AAFAonBpoHeZ2GxmOr7noPtP7VYMbehfmkE"
if not TOKEN:
    logger.error("Не найден токен бота. Установите переменную окружения BOT_TOKEN.")
    exit(1)

# ------------------------- Регулярные выражения для правил -------------------------

# 3.6, 3.7: Запрещённая лексика (открытый мат и его завуалированные варианты)
BANNED_WORDS = [
    'хуй', 'пизда', 'сука', 'блядь', 'дибил', 'гандон', 'еблан',
    'ебать', 'ебал', 'бля', 'блядёшка', 'блядоёбина', 'блядоёбина хуеротая'
]
BANNED_PATTERN = re.compile(r'\b(' + '|'.join(BANNED_WORDS) + r')\b', re.IGNORECASE)

# 3.17: Обнаружение ссылок
LINK_PATTERN = re.compile(
    r'((http|https)://)?'     # протокол (опционально)
    r'([\w.-]+)'              # доменное имя или IP
    r'(\.[a-zA-Z]{2,})'        # TLD
    r'([/\w .-]*)*/*'          # путь
)

# Специфичные ключевые слова для спама/рекламы
SPAM_KEYWORDS = ["зарабатывать", "сотрудничество", "120$", "новичков", "пишит"]

# 3.2-3.4: Пиратство, мошенничество и использование запрещённых программ
FRAUD_PIRACY_PATTERN = re.compile(
    r'\b(мошен(?:ничество)?|пират(?:ский|ство)?|запрещён(?:ные|ные)\s+программ[ы]?|кракер|чита(?:ть|ние)?|бесплатно\s+скачать)\b',
    re.IGNORECASE
)

# 3.5: Выдача себя за представителя администрации
SELF_ADMIN_PATTERN = re.compile(r'\b(я\s*(админ|модератор))\b', re.IGNORECASE)

# 3.13: Пропаганда насилия, оружия, наркотиков, порнографии и т.п.
DRUG_VIOLENCE_PATTERN = re.compile(
    r'\b(наркотик(?:и)?|оружие|насилие|порнография|сексуальный\s+контент|алкоголь|табак)\b',
    re.IGNORECASE
)

# 3.14: Клевета и распространение ложной информации
DEFAMATION_PATTERN = re.compile(r'\b(клевет(?:а)?|ложная\s+информация|фейк)\b', re.IGNORECASE)

# 3.10: Заявления о превосходстве одной нации/народа над другими
SUPERIORITY_PATTERN = re.compile(r'\b(превосходство|лучше\s+всех|супремаси)\b', re.IGNORECASE)

# 3.18: Троллинг и провокационные сообщения
TROLLING_PATTERN = re.compile(r'\b(троллить|троллинг|провокация)\b', re.IGNORECASE)

# 3.20: Разглашение личной информации (телефоны, email и т.п.)
PERSONAL_INFO_PATTERN = re.compile(
    r'\b(\+?\d[\d\s\-]{7,}\d|[\w\.-]+@[\w\.-]+\.\w+)\b'
)

# ------------------------- Вспомогательные функции -------------------------

def has_repeated_characters(text: str) -> bool:
    """Проверяет, содержит ли сообщение более 3-х одинаковых символов подряд (флуд)."""
    return bool(re.search(r'(.)\1{2,}', text))

def contains_link(text: str) -> bool:
    """Проверяет наличие ссылки в сообщении."""
    return bool(LINK_PATTERN.search(text))

def is_mixed_alphabet_spam(text: str, threshold: float = 0.7) -> bool:
    """Проверяет, доминируют ли в сообщении буквы не из кириллицы (признак спама)."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    cyrillic_count = sum(1 for c in letters if re.match(r'[А-Яа-яЁё]', c))
    return (cyrillic_count / len(letters)) < threshold

def log_deleted_message(message) -> None:
    """
    Записывает в файл 'logs.txt' информацию об удалённом сообщении:
    @alias | Ник | дата | сообщение
    Обёртка защищена от ошибок записи.
    """
    try:
        user = message.from_user
        username = "@" + (user.username if user.username else "unknown")
        nickname = user.full_name if user.full_name else "unknown"
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text = message.text
        log_entry = f"{username} | {nickname} | {date_str} | {text}\n"
        with open("logs.txt", "a", encoding="utf-8") as f:
            f.write(log_entry)
    except Exception as e:
        logger.error(f"Ошибка записи в лог-файл: {e}")

async def delete_and_reply(message, reply_text: str) -> None:
    """
    Удаляет сообщение, логирует событие и отправляет уведомление отправителю.
    Оборачивает операции в try/except для устойчивости.
    """
    try:
        await message.delete()
        log_deleted_message(message)
        await message.reply_text(reply_text)
        logger.info(f"Сообщение от {message.from_user.id} удалено. Ответ: {reply_text}")
    except Exception as e:
        logger.error(f"Ошибка при удалении сообщения: {e}")

# ------------------------- Основная функция проверки сообщения -------------------------

async def check_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Обрабатываем только сообщения в группах/супергруппах
    if update.effective_chat.type not in ['group', 'supergroup']:
        return

    message = update.message
    if not message or not message.text:
        return

    # Приводим текст к нижнему регистру для большинства проверок
    text_original = message.text.strip()
    text = text_original.lower()

    # 3.1. Обсуждение правил чата
    if re.search(r'\b(правил(?:а|ы)?\s+чата)\b', text):
        await delete_and_reply(message, "Обсуждение правил чата запрещено.")
        return

    # 3.2-3.4. Мошенничество, пиратство, использование запрещённых программ
    if FRAUD_PIRACY_PATTERN.search(text):
        await delete_and_reply(message, "Обсуждение мошенничества или пиратских ресурсов запрещено.")
        return

    # 3.5. Выдача себя за представителя администрации
    if SELF_ADMIN_PATTERN.search(text):
        await delete_and_reply(message, "Выдача себя за представителя администрации запрещена.")
        return

    # 3.6, 3.7. Использование мата и завуалированного мата
    if BANNED_PATTERN.search(text):
        await delete_and_reply(message, "Ваше сообщение удалено за использование ненормативной лексики.")
        return

    # 3.8. Дискриминация и националистические высказывания
    hate_keywords = ['нация', 'рас', 'евреи', 'черные', 'азиаты', 'национализм', 'супремаси']
    if any(word in text for word in hate_keywords):
        await delete_and_reply(message, "Ваше сообщение удалено за недопустимое содержание.")
        return

    # 3.10. Заявления о превосходстве (если выявлены, удаляем сообщение)
    if SUPERIORITY_PATTERN.search(text):
        await delete_and_reply(message, "Выражения превосходства над другими запрещены.")
        return

    # 3.13. Пропаганда насилия, оружия, наркотиков, порнографии и т.п.
    if DRUG_VIOLENCE_PATTERN.search(text):
        await delete_and_reply(message, "Пропаганда насилия, оружия, наркотиков или порнографического контента запрещена.")
        return

    # 3.14. Клевета и распространение ложной информации
    if DEFAMATION_PATTERN.search(text):
        await delete_and_reply(message, "Распространение ложной информации и клевета запрещены.")
        return

    # 3.15, 3.21. Угрозы, оскорбления и провокационные высказывания
    threat_keywords = ['убью', 'смерть', 'угрожаю', 'накажу']
    if any(word in text for word in threat_keywords):
        await delete_and_reply(message, "Ваше сообщение удалено за угрозы или оскорбления.")
        return

    # 3.16. Флуд: повторяющиеся символы или слишком частые сообщения
    if has_repeated_characters(text):
        await delete_and_reply(message, "Ваше сообщение удалено за флуд (повторяющиеся символы).")
        return

    # 3.17. Спам, реклама и публикация ссылок
    if contains_link(text):
        await delete_and_reply(message, "Ваше сообщение удалено за публикацию ссылок/рекламу.")
        return

    # 3.18. Троллинг и провокационные сообщения
    if TROLLING_PATTERN.search(text):
        await delete_and_reply(message, "Троллинг и провокационные сообщения запрещены.")
        return

    # 3.19. Обсуждение действий модераторов/администрации
    if (re.search(r'\b(модератор(ы)?|администрация)\b', text) and
        re.search(r'\b(обсуждение|критику(?:ть)?|жалоба)\b', text)):
        await delete_and_reply(message, "Обсуждение действий модераторов запрещено.")
        return

    # 3.20. Разглашение личной информации
    if PERSONAL_INFO_PATTERN.search(text):
        await delete_and_reply(message, "Разглашение личной информации запрещено.")
        return

    # 3.22. Попрошайничество
    if re.search(r'\b(пожалуйста,? дайте|помогите мне,? пожалуйста)\b', text):
        await delete_and_reply(message, "Попрошайничество запрещено.")
        return

    # 3.23. Нытьё и гнусавость
    if re.search(r'\b(хныкать|ныть|жаловаться без причины)\b', text):
        await delete_and_reply(message, "Нытьё в чате не приветствуется.")
        return

    # 4.1. Злоупотребление CAPS LOCK (если более 70% букв — заглавные)
    letters = [c for c in text if c.isalpha()]
    if letters:
        uppercase_count = sum(1 for c in letters if c.isupper())
        if len(letters) > 10 and (uppercase_count / len(letters)) > 0.7:
            await delete_and_reply(message, "Злоупотребление CAPS LOCK запрещено.")
            return

    # 4.2. Избыточное использование emoji (если сообщение состоит только из emoji и их слишком много)
    text_no_space = text_original.replace(" ", "")
    emoji_pattern = re.compile(
        "[\U0001F600-\U0001F64F"
        "\U0001F300-\U0001F5FF"
        "\U0001F680-\U0001F6FF"
        "\U0001F1E0-\U0001F1FF]+", flags=re.UNICODE
    )
    if text_no_space and emoji_pattern.fullmatch(text_no_space):
        count = context.user_data.get('emoji_count', 0) + 1
        context.user_data['emoji_count'] = count
        if count >= 5:
            await delete_and_reply(message, "Избыточное использование emoji не приветствуется.")
            context.user_data['emoji_count'] = 0
            return
    else:
        context.user_data['emoji_count'] = 0

    # 4.4. Разбиение сообщения на множество коротких слов
    words = text.split()
    if len(words) >= 5 and all(len(word) <= 2 for word in words):
        await delete_and_reply(message, "Разбивание сообщения на отдельные слова недопустимо.")
        return

    # 4.5. Сообщения должны быть на русском языке (проверяем соотношение кириллицы)
    total_letters = [c for c in text if c.isalpha()]
    if total_letters:
        cyrillic_count = sum(1 for c in total_letters if re.match(r'[А-Яа-яЁё]', c))
        if (cyrillic_count / len(total_letters)) < 0.5:
            await delete_and_reply(message, "Сообщения должны быть на русском языке.")
            return

    # Дополнительная проверка на спам (смешение алфавитов)
    if any(keyword in text for keyword in SPAM_KEYWORDS) and is_mixed_alphabet_spam(text):
        await delete_and_reply(message, "Сообщение удалено как спам.")
        return

    # Ограничение частоты сообщений: более 3 сообщений за 3 минуты
    now = datetime.now()
    message_times = context.user_data.get('message_times', [])
    message_times = [t for t in message_times if now - t < timedelta(minutes=3)]
    message_times.append(now)
    context.user_data['message_times'] = message_times
    if len(message_times) > 3:
        await delete_and_reply(message, "Вы отправляете сообщения слишком часто. Пожалуйста, замедлитесь.")
        context.user_data['message_times'] = []
        return

# ------------------------- Команды бота -------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот-модератор чата. Соблюдайте правила:\n"
        "- Ненормативная лексика, угрозы и оскорбления запрещены.\n"
        "- Флуд, спам, ссылки и реклама не приветствуются.\n"
        "- Обсуждение правил и действий модераторов запрещено.\n"
        "Более подробные правила доступны у администратора."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Список доступных команд:\n"
        "/start — запуск бота\n"
        "/rules — правила чата\n"
        "/help — помощь"
    )

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rules_text = (
        "📚 Правила чата:\n\n"
        "⚠️ 1. Условия действия правил чата\n"
        "  1.1. Пользователи, заходя в чат, принимают на себя обязательства соблюдать правила.\n"
        "  1.2. Незнание правил не освобождает от ответственности.\n\n"
        "✅ 2. Разрешается общение, шутки, помощь и обмен информацией.\n\n"
        "⛔ 3. Категорически запрещено:\n"
        "  3.1. Обсуждение правил чата.\n"
        "  3.2. Мошенничество, пиратство и использование запрещённых программ.\n"
        "  3.3. Выдача себя за представителя администрации.\n"
        "  3.4. Использование мата и завуалированной лексики.\n"
        "  3.5. Дискриминация, национализм и разжигание ненависти.\n"
        "  3.6. Флуд, спам, реклама и публикация ссылок.\n"
        "  3.7. Троллинг, провокации и обсуждение действий модераторов.\n"
        "  3.8. Разглашение личной информации.\n"
        "  3.9. Угрозы, попрошайничество, нытьё и гнусавость.\n\n"
        "⚠️ 4. Рекомендуется не злоупотреблять Caps Lock, смайлами, разбивать сообщения и использовать только русский язык (если нет необходимости).\n\n"
        "✅ 5. Emoji допустимы для выражения эмоций, но их избыточное использование недопустимо."
    )
    await update.message.reply_text(rules_text)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(msg="Ошибка при обработке обновления", exc_info=context.error)

# ------------------------- Основной запуск -------------------------

def main():
    application = Application.builder().token(TOKEN).build()

    # Регистрируем обработчики для команд в групповых чатах
    application.add_handler(CommandHandler("start", start, filters=filters.ChatType.GROUPS))
    application.add_handler(CommandHandler("rules", rules, filters=filters.ChatType.GROUPS))
    application.add_handler(CommandHandler("help", help_command, filters=filters.ChatType.GROUPS))
    # Обработчик текстовых сообщений (исключая команды) с проверками правил
    application.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, check_message))
    application.add_error_handler(error_handler)

    application.run_polling()

if __name__ == '__main__':
    main()