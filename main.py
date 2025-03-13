import logging
import psycopg2
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

# ------------------------------
# Настройки и глобальные переменные
# ------------------------------
# Параметры подключения к PostgreSQL
DB_HOST = "localhost"
DB_NAME = "frontendtgbot_db"
DB_USER = "your_db_user"  # замените на ваше имя пользователя БД
DB_PASS = "your_db_password"  # замените на ваш пароль БД

# ID чата (или thread) для логов в группе Frontend telegram admin
LOGS_CHAT_ID = -123456789  # замените на реальный ID лога

# Список ID администраторов
ADMINS = [111111111, 222222222]  # замените на реальные ID админов

# ID разрешённого чата, куда бот может быть добавлен
ALLOWED_CHAT_ID = -987654321  # замените на нужный ID

# Путь к файлу с правилами (на виртуальной машине по адресу /root/frontendtgbot)
RULES_PATH = "/root/frontendtgbot/rules.txt"

# ------------------------------
# Логирование
# ------------------------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# ------------------------------
# Инициализация подключения к базе данных
# ------------------------------
conn = psycopg2.connect(host=DB_HOST, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
cursor = conn.cursor()

# Создаём таблицу для хранения предупреждений, если её ещё нет
cursor.execute("""
CREATE TABLE IF NOT EXISTS warnings (
    user_id BIGINT PRIMARY KEY,
    warnings_count INTEGER DEFAULT 0,
    last_warning TIMESTAMP
)
""")
conn.commit()


# ------------------------------
# Функции для работы с базой данных
# ------------------------------
def add_warning(user_id: int, punishment_days: int) -> int:
    """Добавляет предупреждение пользователю с указанием срока наказания."""
    now = datetime.utcnow()
    cursor.execute("SELECT warnings_count FROM warnings WHERE user_id = %s", (user_id,))
    row = cursor.fetchone()
    if row:
        warnings_count = row[0] + 1
        cursor.execute("UPDATE warnings SET warnings_count = %s, last_warning = %s WHERE user_id = %s",
                       (warnings_count, now, user_id))
    else:
        warnings_count = 1
        cursor.execute("INSERT INTO warnings (user_id, warnings_count, last_warning) VALUES (%s, %s, %s)",
                       (user_id, warnings_count, now))
    conn.commit()
    return warnings_count


def get_warning_info(user_id: int):
    """Возвращает информацию о предупреждениях пользователя."""
    cursor.execute("SELECT warnings_count, last_warning FROM warnings WHERE user_id = %s", (user_id,))
    return cursor.fetchone()


# ------------------------------
# Обработчики команд и событий
# ------------------------------
def chatid_handler(update: Update, context: CallbackContext):
    """
    Команда для отладки. Выводит id текущего чата.
    Чтобы получить id чата, можно использовать update.effective_chat.id.
    """
    chat_id = update.effective_chat.id
    update.message.reply_text(f"ID чата: {chat_id}")


def rules_handler(update: Update, context: CallbackContext):
    """
    Выводит полный текст правил из файла.
    Правила доступны для просмотра только в личном чате с ботом.
    """
    # Проверка типа чата
    if update.effective_chat.type != "private":
        update.message.reply_text("Команда /rules доступна только в личных сообщениях с ботом.")
        return

    try:
        with open(RULES_PATH, "r", encoding="utf-8") as f:
            rules_text = f.read()
        update.message.reply_text(rules_text)
    except Exception as e:
        update.message.reply_text("Ошибка при чтении файла с правилами.")
        logger.error(f"Ошибка чтения {RULES_PATH}: {e}")


def warn_handler(update: Update, context: CallbackContext):
    """
    Команда для выдачи предупреждения пользователю.
    Использование: /warn <user_id> [days]
    """
    if update.effective_user.id not in ADMINS:
        update.message.reply_text("У вас нет прав для выполнения этой команды.")
        return

    if not context.args:
        update.message.reply_text("Используйте: /warn <user_id> [days]")
        return

    try:
        user_id = int(context.args[0])
        punishment_days = int(context.args[1]) if len(context.args) > 1 else 3
    except ValueError:
        update.message.reply_text("Ошибка: user_id и days должны быть числами.")
        return

    warnings_count = add_warning(user_id, punishment_days)
    log_msg = (f"Пользователь {user_id} получил предупреждение от администратора {update.effective_user.id}. "
               f"Всего предупреждений: {warnings_count}. Наказание действует {punishment_days} дней.")
    # Логирование в специальный чат
    context.bot.send_message(chat_id=LOGS_CHAT_ID, text=log_msg)
    update.message.reply_text("Предупреждение выдано.")


def ban_handler(update: Update, context: CallbackContext):
    """
    Команда для бана пользователя.
    Использование: /ban <user_id>
    """
    if update.effective_user.id not in ADMINS:
        update.message.reply_text("У вас нет прав для выполнения этой команды.")
        return

    if not context.args:
        update.message.reply_text("Используйте: /ban <user_id>")
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        update.message.reply_text("Ошибка: user_id должен быть числом.")
        return

    try:
        context.bot.kick_chat_member(chat_id=update.effective_chat.id, user_id=user_id)
        log_msg = f"Пользователь {user_id} был забанен администратором {update.effective_user.id}."
        context.bot.send_message(chat_id=LOGS_CHAT_ID, text=log_msg)
        update.message.reply_text("Пользователь забанен.")
    except Exception as e:
        update.message.reply_text(f"Ошибка: {e}")
        logger.error(f"Ошибка при бане пользователя {user_id}: {e}")


def message_violation_handler(update: Update, context: CallbackContext):
    """
    Обработчик для фиксации нарушений.
    Если сообщение нарушает правила, бот должен отправлять уведомление с упоминанием нарушителя и логировать событие.
    Внимание: Telegram API не присылает уведомления о удалённых сообщениях, поэтому эту функцию необходимо
    вызывать в момент фиксации нарушения (например, модератором или дополнительной логикой).
    """
    violator = update.effective_user
    violation_msg = (f"@{violator.username if violator.username else violator.id} нарушил(а) правила! "
                     "Сообщение удалено.")
    # Отправляем уведомление в тот же чат
    context.bot.send_message(chat_id=update.effective_chat.id, text=violation_msg)
    # Логируем событие в специальном чате
    context.bot.send_message(chat_id=LOGS_CHAT_ID, text=violation_msg)


def new_chat_member_handler(update: Update, context: CallbackContext):
    """
    Обработчик новых участников.
    Если бот добавлен в группу, не являющуюся разрешённой, он покинет группу.
    """
    for member in update.message.new_chat_members:
        if member.id == context.bot.id:
            if update.effective_chat.id != ALLOWED_CHAT_ID:
                update.message.reply_text("Я не предназначен для этой группы. Прощайте!")
                context.bot.leave_chat(chat_id=update.effective_chat.id)
                logger.info(f"Бот покинул группу {update.effective_chat.id} – неразрешённая группа.")


# Дополнительное улучшение:
# Функция для планирования автоматического сброса предупреждений по истечении срока наказания.
# Для этого можно использовать библиотеку APScheduler. Ниже приведён пример шаблона.
def schedule_warning_reset():
    """
    Пример функции, которую можно запланировать для проверки и сброса предупреждений.
    Реальная реализация зависит от выбранного планировщика (например, APScheduler).
    """
    cursor.execute("SELECT user_id, last_warning FROM warnings")
    for user_id, last_warning in cursor.fetchall():
        # Если с момента последнего предупреждения прошло больше 14 дней, сбрасываем предупреждения
        if datetime.utcnow() - last_warning > timedelta(days=14):
            cursor.execute("UPDATE warnings SET warnings_count = 0 WHERE user_id = %s", (user_id,))
    conn.commit()
    logger.info("Проверка и сброс предупреждений завершена.")


# ------------------------------
# Основная функция запуска бота
# ------------------------------
def main():
    # Инициализация Updater с токеном
    updater = Updater("YOUR_BOT_TOKEN", use_context=True)  # замените YOUR_BOT_TOKEN на реальный токен
    dp = updater.dispatcher

    # Команды для администраторов и отладки
    dp.add_handler(CommandHandler("chatid", chatid_handler))
    dp.add_handler(CommandHandler("warn", warn_handler))
    dp.add_handler(CommandHandler("ban", ban_handler))

    # Команда для просмотра правил – доступна только в ЛС
    dp.add_handler(CommandHandler("rules", rules_handler))

    # Обработчик для новых участников (и для запрета добавления бота в неразрешённые группы)
    dp.add_handler(MessageHandler(Filters.status_update.new_chat_members, new_chat_member_handler))

    # Обработчик для фиксации нарушений (здесь можно расширять логику обнаружения нарушений)
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, message_violation_handler))

    # Здесь можно добавить планировщик (например, APScheduler) для вызова schedule_warning_reset() по расписанию

    updater.start_polling()
    updater.idle()


if __name__ == '__main__':
    main()