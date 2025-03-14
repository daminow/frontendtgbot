#!/usr/bin/env python3
import re
import json
import logging
import os
import psycopg2
import threading
import asyncio
from datetime import datetime, timedelta

from telegram import Update, ChatPermissions
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

# ========= Конфигурация =========
TOKEN = "7501357038:AAFAonBpoHeZ2GxmOr7noPtP7VYMbehfmkE"
# Если используются супергруппы, ID обычно имеют вид -100XXXXXXXXXX
FRONTEND_CHAT_ID = -1002609344415  # Чат пользователей
ADMIN_GROUP_ID = -1002609344415     # Группа "Frontend администрация"
BOT_THREAD_ID = 98                 # ID темы для команд (тема "bot")
LOGS_THREAD_ID = 88                # ID темы для логов (тема "logs")
DATABASE_URL = "postgresql://frontendtgbot_db_admin:1509@localhost/frontendtgbot_db"
SCHEMA = "my_schema"

# ========= Настройка логирования =========
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========= Работа с базой данных (PostgreSQL) =========
def init_db_postgres():
    """Создаёт схему (если её нет) и таблицу для хранения информации о пользователях."""
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE SCHEMA IF NOT EXISTS {SCHEMA} AUTHORIZATION frontendtgbot_db_admin;"
                )
                cur.execute(
                    f'''
                    CREATE TABLE IF NOT EXISTS {SCHEMA}.users (
                        user_id BIGINT PRIMARY KEY,
                        join_date TIMESTAMP,
                        alias TEXT,
                        warns INTEGER DEFAULT 0,
                        bans INTEGER DEFAULT 0,
                        history JSONB
                    );
                    '''
                )
                conn.commit()
        logger.info("Схема и таблица успешно созданы или уже существуют.")
    except Exception as e:
        logger.error("Ошибка инициализации БД: %s", e)


def get_user(user_id: int):
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT user_id, join_date, alias, warns, bans, history "
                    f"FROM {SCHEMA}.users WHERE user_id = %s;",
                    (user_id,),
                )
                row = cur.fetchone()
                if row:
                    history = row[5]
                    if isinstance(history, str):
                        try:
                            history = json.loads(history)
                        except Exception:
                            history = []
                    return {
                        "user_id": row[0],
                        "join_date": row[1],
                        "alias": row[2],
                        "warns": row[3],
                        "bans": row[4],
                        "history": history if history else [],
                    }
    except Exception as e:
        logger.error("Ошибка в get_user: %s", e)
    return None


def create_user(user_id: int, alias: str):
    now = datetime.now()
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f'''
                    INSERT INTO {SCHEMA}.users 
                        (user_id, join_date, alias, warns, bans, history)
                    VALUES (%s, %s, %s, 0, 0, %s)
                    ON CONFLICT (user_id) DO NOTHING;
                    ''',
                    (user_id, now, alias, json.dumps([])),
                )
                conn.commit()
    except Exception as e:
        logger.error("Ошибка в create_user: %s", e)


def update_user(user: dict):
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f'''
                    UPDATE {SCHEMA}.users
                    SET alias = %s, warns = %s, bans = %s, history = %s
                    WHERE user_id = %s;
                    ''',
                    (
                        user["alias"],
                        user["warns"],
                        user["bans"],
                        json.dumps(user["history"]),
                        user["user_id"],
                    ),
                )
                conn.commit()
    except Exception as e:
        logger.error("Ошибка в update_user: %s", e)


def add_punishment(
        user_id: int,
        alias: str,
        punishment_type: str,
        reason: str,
        duration: int,
        issued_by: str,
):
    """
    Добавляет наказание в историю пользователя.
    punishment_type: "warn", "ban", "mute" и т.п.
    duration: срок наказания (в днях для warn, в часах для ban, в минутах для mute)
    issued_by: имя администратора или 'bot'
    """
    now = datetime.now()
    punishment = {
        "id": int(now.timestamp()),
        "date": now.strftime("%Y-%m-%d %H:%M:%S"),
        "type": punishment_type,
        "reason": reason,
        "duration": duration,
        "issued_by": issued_by,
    }
    user_record = get_user(user_id)
    if not user_record:
        create_user(user_id, alias)
        user_record = get_user(user_id)
    if punishment_type == "warn":
        user_record["warns"] += 1
    elif punishment_type == "ban":
        user_record["bans"] += 1
    user_record["history"].append(punishment)
    update_user(user_record)
    return user_record


def remove_warn(user_id: int):
    """Удаляет одно предупреждение из истории пользователя."""
    user_record = get_user(user_id)
    if not user_record or user_record["warns"] <= 0:
        return None
    user_record["warns"] -= 1
    for i in range(len(user_record["history"]) - 1, -1, -1):
        if user_record["history"][i].get("type") == "warn":
            del user_record["history"][i]
            break
    update_user(user_record)
    return user_record


def cleanup_expired_warnings():
    """Периодическая очистка устаревших предупреждений.
    Пока функция является заглушкой и запускается каждые 3600 секунд.
    """
    logger.info("Запущена задача очистки устаревших предупреждений.")
    threading.Timer(3600, cleanup_expired_warnings).start()


# ========= Работа со списком запрещённых слов =========
def load_banned_keywords():
    try:
        with open("banned_keywords.txt", "r", encoding="utf-8") as f:
            keywords = [
                line.strip() for line in f if line.strip() and not line.startswith("#")
            ]
        logger.info("Загружен список запрещённых слов.")
        return keywords
    except Exception as e:
        logger.error("Ошибка загрузки banned_keywords.txt: %s", e)
        return ["запрещенное_слово1", "запрещенное_слово2"]


BANNED_KEYWORDS = load_banned_keywords()


def check_violation(text: str) -> bool:
    text_lower = text.lower()
    for word in BANNED_KEYWORDS:
        pattern = r"\b" + re.escape(word.lower()) + r"\b"
        if re.search(pattern, text_lower):
            return True
    return False


# ========= Функция автоматического разбана =========
async def schedule_unban(bot, user_id: int, duration: int):
    """После истечения срока бана (в часах) автоматически разбанивает пользователя."""
    await asyncio.sleep(duration * 3600)
    try:
        await bot.unban_chat_member(
            chat_id=FRONTEND_CHAT_ID, user_id=user_id, only_if_banned=True
        )
        logger.info(f"Пользователь {user_id} автоматически разбанен после {duration} часов.")
    except Exception as e:
        logger.error("Ошибка автоматического разбана пользователя %s: %s", user_id, e)


# ========= Функция запуска =========
async def on_startup(app):
    try:
        await app.bot.send_message(
            chat_id=FRONTEND_CHAT_ID,
            text="Бот подключен в основной чат."
        )
        await app.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            message_thread_id=BOT_THREAD_ID,
            text="Бот подключен в тему команд.",
        )
        await app.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            message_thread_id=LOGS_THREAD_ID,
            text="Бот подключен в тему логов.",
        )
        logger.info("Сообщения о подключении успешно отправлены.")
    except Exception as e:
        logger.error("Ошибка отправки сообщений о подключении: %s", e)


# ========= Проверка прав администратора =========
async def is_admin_in_frontend(
        user_id: int, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    try:
        admins = await context.bot.get_chat_administrators(FRONTEND_CHAT_ID)
        admin_ids = {admin.user.id for admin in admins}
        return user_id in admin_ids
    except Exception as e:
        logger.error("Ошибка при получении администраторов FRONTEND_CHAT_ID: %s", e)
        return False


async def is_valid_admin_command(
        update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """
    Команда действительна, если:
      - сообщение отправлено в группе ADMIN_GROUP_ID,
      - в теме с ID BOT_THREAD_ID,
      - отправитель является администратором в FRONTEND_CHAT_ID.
    """
    message = update.effective_message
    if message.chat.id != ADMIN_GROUP_ID:
        return False
    if not message.message_thread_id or message.message_thread_id != BOT_THREAD_ID:
        return False
    return await is_admin_in_frontend(message.from_user.id, context)


async def delete_command_message(update: Update):
    try:
        await update.effective_message.delete()
    except Exception as e:
        logger.error("Ошибка удаления командного сообщения: %s", e)


# ========= Обработчики сообщений =========
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    При обнаружении нарушения бот удаляет сообщение, уведомляет пользователя,
    выдаёт предупреждение и логирует событие.
    """
    message = update.effective_message
    if not message or not message.text:
        return

    if check_violation(message.text):
        user_tag = (
            f"@{message.from_user.username}"
            if message.from_user.username
            else message.from_user.first_name
        )
        violation_notice = f"{user_tag}, ваше сообщение нарушает правила чата."
        try:
            await message.delete()
        except Exception as e:
            logger.error("Ошибка удаления сообщения: %s", e)
        try:
            await context.bot.send_message(
                chat_id=message.chat.id,
                text=violation_notice,
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error("Ошибка отправки уведомления: %s", e)
        user_record = add_punishment(
            message.from_user.id, user_tag, "warn", "Нарушение правил", 3, "bot"
        )
        try:
            await context.bot.send_message(
                chat_id=message.from_user.id,
                text=(
                    f"Вы получили предупреждение. Всего предупреждений: "
                    f"{user_record['warns']}."
                ),
            )
        except Exception as e:
            logger.error("Ошибка отправки личного сообщения: %s", e)
        log_text = (
            f"Автоматическое предупреждение: пользователь {user_tag} "
            f"(ID: {message.from_user.id}) нарушил правила. Всего предупреждений: "
            f"{user_record['warns']}."
        )
        await context.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            message_thread_id=LOGS_THREAD_ID,
            text=log_text,
        )


# ========= Команды для администраторов =========
async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /ban user_id причина срок(в часах)
    Выдаёт бан пользователю в FRONTEND_CHAT_ID, фиксирует наказание и
    автоматически снимает бан по истечении срока.
    """
    if not await is_valid_admin_command(update, context):
        return
    args = context.args
    if len(args) < 3:
        await update.effective_message.reply_text(
            "Использование: /ban user_id причина срок(в часах)"
        )
        await delete_command_message(update)
        return

    target_alias = args[0].lstrip("@")
    try:
        duration = int(args[-1])
    except ValueError:
        await update.effective_message.reply_text(
            "Срок должен быть числом (в часах)."
        )
        await delete_command_message(update)
        return
    reason = " ".join(args[1:-1])
    try:
        target_user_id = int(target_alias)
    except ValueError:
        await update.effective_message.reply_text(
            "Ошибка: для /ban необходимо указывать числовой user_id."
        )
        await delete_command_message(update)
        return
    try:
        await context.bot.ban_chat_member(
            chat_id=FRONTEND_CHAT_ID, user_id=target_user_id
        )
        user_record = add_punishment(
            target_user_id,
            target_alias,
            "ban",
            reason,
            duration,
            update.effective_message.from_user.first_name,
        )
        await update.effective_message.reply_text(
            f"Пользователь {target_alias} забанен на {duration} часов. "
            f"Всего банов: {user_record['bans']}."
        )
        log_text = (
            f"БАН: Админ {update.effective_message.from_user.first_name} "
            f"(ID: {update.effective_message.from_user.id}) забанил пользователя {target_alias} "
            f"на {duration} часов. Причина: {reason}. Всего банов: {user_record['bans']}."
        )
        await context.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            message_thread_id=LOGS_THREAD_ID,
            text=log_text,
        )
        asyncio.create_task(schedule_unban(context.bot, target_user_id, duration))
    except Exception as e:
        await update.effective_message.reply_text(
            "Ошибка при выполнении команды /ban."
        )
        logger.error("Ошибка в /ban: %s", e)
    await delete_command_message(update)


async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /warn user_id причина срок(в днях)
    Выдаёт предупреждение пользователю.
    """
    if not await is_valid_admin_command(update, context):
        return
    args = context.args
    if len(args) < 3:
        await update.effective_message.reply_text(
            "Использование: /warn user_id причина срок(в днях)"
        )
        await delete_command_message(update)
        return

    target_alias = args[0].lstrip("@")
    try:
        duration = int(args[-1])
    except ValueError:
        await update.effective_message.reply_text(
            "Срок должен быть числом (в днях)."
        )
        await delete_command_message(update)
        return
    reason = " ".join(args[1:-1])
    try:
        target_user_id = int(target_alias)
    except ValueError:
        await update.effective_message.reply_text(
            "Ошибка: для /warn необходимо указывать числовой user_id."
        )
        await delete_command_message(update)
        return
    try:
        user_record = add_punishment(
            target_user_id,
            target_alias,
            "warn",
            reason,
            duration,
            update.effective_message.from_user.first_name,
        )
        await update.effective_message.reply_text(
            f"Пользователю {target_alias} выдано предупреждение. Всего предупреждений: "
            f"{user_record['warns']}."
        )
        log_text = (
            f"ПРЕДУПРЕЖДЕНИЕ: Админ {update.effective_message.from_user.first_name} "
            f"(ID: {update.effective_message.from_user.id}) выдал предупреждение пользователю "
            f"{target_alias} на {duration} дней. Причина: {reason}. Всего предупреждений: "
            f"{user_record['warns']}."
        )
        await context.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            message_thread_id=LOGS_THREAD_ID,
            text=log_text,
        )
    except Exception as e:
        await update.effective_message.reply_text(
            "Ошибка при выполнении команды /warn."
        )
        logger.error("Ошибка в /warn: %s", e)
    await delete_command_message(update)


async def unwarn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /unwarn user_id
    Снимает одно предупреждение с пользователя.
    """
    if not await is_valid_admin_command(update, context):
        return
    args = context.args
    if len(args) < 1:
        await update.effective_message.reply_text("Использование: /unwarn user_id")
        await delete_command_message(update)
        return

    target_alias = args[0].lstrip("@")
    try:
        target_user_id = int(target_alias)
    except ValueError:
        await update.effective_message.reply_text(
            "Ошибка: для /unwarn необходимо указывать числовой user_id."
        )
        await delete_command_message(update)
        return
    user_record = remove_warn(target_user_id)
    if user_record is None:
        await update.effective_message.reply_text("Нет предупреждений для снятия.")
    else:
        await update.effective_message.reply_text(
            f"Предупреждение снято. Осталось предупреждений: {user_record['warns']}."
        )
        log_text = (
            f"UNWARN: Админ {update.effective_message.from_user.first_name} "
            f"(ID: {update.effective_message.from_user.id}) снял предупреждение с пользователя "
            f"{target_alias}. Осталось предупреждений: {user_record['warns']}."
        )
        await context.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            message_thread_id=LOGS_THREAD_ID,
            text=log_text,
        )
    await delete_command_message(update)


async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /mute user_id причина срок(в минутах)
    Ограничивает возможность отправки сообщений в FRONTEND_CHAT_ID.
    """
    if not await is_valid_admin_command(update, context):
        return
    args = context.args
    if len(args) < 3:
        await update.effective_message.reply_text(
            "Использование: /mute user_id причина срок(в минутах)"
        )
        await delete_command_message(update)
        return

    target_alias = args[0].lstrip("@")
    try:
        duration = int(args[-1])
    except ValueError:
        await update.effective_message.reply_text(
            "Срок должен быть числом (в минутах)."
        )
        await delete_command_message(update)
        return
    reason = " ".join(args[1:-1])
    try:
        target_user_id = int(target_alias)
    except ValueError:
        await update.effective_message.reply_text(
            "Ошибка: для /mute необходимо указывать числовой user_id."
        )
        await delete_command_message(update)
        return
    try:
        until_date = datetime.now() + timedelta(minutes=duration)
        permissions = ChatPermissions(can_send_messages=False)
        await context.bot.restrict_chat_member(
            chat_id=FRONTEND_CHAT_ID,
            user_id=target_user_id,
            permissions=permissions,
            until_date=until_date,
        )
        user_record = add_punishment(
            target_user_id,
            target_alias,
            "mute",
            reason,
            duration,
            update.effective_message.from_user.first_name,
        )
        await update.effective_message.reply_text(
            f"Пользователь {target_alias} замьючен на {duration} минут."
        )
        log_text = (
            f"MUTE: Админ {update.effective_message.from_user.first_name} "
            f"(ID: {update.effective_message.from_user.id}) замьючил пользователя "
            f"{target_alias} на {duration} минут. Причина: {reason}."
        )
        await context.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            message_thread_id=LOGS_THREAD_ID,
            text=log_text,
        )
    except Exception as e:
        await update.effective_message.reply_text(
            "Ошибка при выполнении команды /mute."
        )
        logger.error("Ошибка в /mute: %s", e)
    await delete_command_message(update)


async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /unmute user_id
    Снимает ограничения (unmute) с пользователя.
    """
    if not await is_valid_admin_command(update, context):
        return
    args = context.args
    if len(args) < 1:
        await update.effective_message.reply_text("Использование: /unmute user_id")
        await delete_command_message(update)
        return

    target_alias = args[0].lstrip("@")
    try:
        target_user_id = int(target_alias)
    except ValueError:
        await update.effective_message.reply_text(
            "Ошибка: для /unmute необходимо указывать числовой user_id."
        )
        await delete_command_message(update)
        return
    try:
        permissions = ChatPermissions(can_send_messages=True)
        await context.bot.restrict_chat_member(
            chat_id=FRONTEND_CHAT_ID,
            user_id=target_user_id,
            permissions=permissions,
        )
        await update.effective_message.reply_text(
            f"Ограничения сняты с пользователя {target_alias}."
        )
        log_text = (
            f"UNMUTE: Админ {update.effective_message.from_user.first_name} "
            f"(ID: {update.effective_message.from_user.id}) снял ограничения с пользователя "
            f"{target_alias}."
        )
        await context.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            message_thread_id=LOGS_THREAD_ID,
            text=log_text,
        )
    except Exception as e:
        await update.effective_message.reply_text(
            "Ошибка при выполнении команды /unmute."
        )
        logger.error("Ошибка в /unmute: %s", e)
    await delete_command_message(update)


async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Выводит правила чата.
    Команда доступна только в личном чате.
    """
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text(
            "Команда /rules доступна только в личном чате со мной. "
            "Пожалуйста, напишите мне в ЛС."
        )
        return

    rules_file = "rules.txt"
    if not os.path.exists(rules_file):
        # Если файла нет, можно задать правила по умолчанию
        default_rules = "Правила чата:\n1. Будьте вежливы.\n2. Не допускаются оскорбления.\n3. Соблюдайте тему."
        await update.effective_message.reply_text(default_rules)
        return

    try:
        with open(rules_file, "r", encoding="utf-8") as f:
            rules_text = f.read()
        await update.effective_message.reply_text(rules_text)
    except Exception as e:
        await update.effective_message.reply_text("Не удалось загрузить правила.")
        logger.error("Ошибка при чтении rules.txt: %s", e)


async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Приветствие новых участников.
    """
    for member in update.effective_message.new_chat_members:
        welcome_text = (
            f"Добро пожаловать, {member.first_name}! "
            "Ознакомьтесь с правилами чата."
        )
        await context.bot.send_message(
            chat_id=update.effective_message.chat.id, text=welcome_text
        )


async def prevent_group_addition(
        update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """
    Если бот добавлен в группу, он сообщает, что не предназначен для работы в группах,
    и покидает её.
    """
    chat_member_update = update.my_chat_member
    if not chat_member_update:
        return
    chat = chat_member_update.chat
    new_status = chat_member_update.new_chat_member.status
    if chat.type != ChatType.PRIVATE and new_status in ["member", "administrator"]:
        await context.bot.send_message(
            chat_id=chat.id,
            text="Извините, я не предназначен для работы в группах."
        )
        await context.bot.leave_chat(chat.id)


# ========= Основная функция =========
async def main():
    init_db_postgres()
    cleanup_expired_warnings()

    application = ApplicationBuilder().token(TOKEN).build()

    # Регистрируем обработчики команд администрирования (работают в группе)
    application.add_handler(
        CommandHandler("ban", ban_command, filters=filters.ChatType.GROUP)
    )
    application.add_handler(
        CommandHandler("warn", warn_command, filters=filters.ChatType.GROUP)
    )
    application.add_handler(
        CommandHandler("unwarn", unwarn_command, filters=filters.ChatType.GROUP)
    )
    application.add_handler(
        CommandHandler("mute", mute_command, filters=filters.ChatType.GROUP)
    )
    application.add_handler(
        CommandHandler("unmute", unmute_command, filters=filters.ChatType.GROUP)
    )
    # Обработчик команды /rules (работает только в ЛС)
    application.add_handler(CommandHandler("rules", rules_command))
    # Обработчик обычных сообщений для автоматической проверки нарушений
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    # Приветствие новых участников
    application.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member)
    )
    # Обработчик изменения статуса бота (автоматическое покидание групп)
    application.add_handler(
        ChatMemberHandler(prevent_group_addition,
                          ChatMemberHandler.MY_CHAT_MEMBER)
    )

    # Используем последовательный запуск polling вместо run_polling, чтобы избежать ошибок с циклом событий
    await application.initialize()
    await on_startup(application)
    await application.start_polling()
    await application.updater.idle()


if __name__ == "__main__":
    asyncio.run(main())