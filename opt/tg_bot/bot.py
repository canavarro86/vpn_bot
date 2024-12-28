import asyncio
import logging
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

load_dotenv()
API_TOKEN = os.getenv("API_TOKEN")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

DB_PATH = "base/vpn_users.db"
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

FREE_TARIFF = {
    "traffic_limit_mb": 10240,  # 10GB
    "duration_days": 30
}

GENERATE_SCRIPT = "/opt/tg-bot/generate_user_config.sh"
CLEANING_SCRIPT = "/opt/tg-bot/clean_ovpn.sh"
CONFIG_DIR = "/etc/openvpn/client"

HELP_GUIDES = {
    "iPhone": "1. Установите приложение OpenVPN Connect из App Store.\n2. Перенесите файл конфигурации .ovpn на устройство через почту, мессенджер или iCloud.\n3. Откройте файл в приложении OpenVPN Connect и импортируйте его.\n4. Включите VPN, нажав 'Connect'.",
    "Android": "1. Установите приложение OpenVPN Connect из Google Play.\n2. Перенесите файл конфигурации .ovpn на устройство через почту, мессенджер или Google Drive.\n3. Откройте файл в приложении OpenVPN Connect и импортируйте его.\n4. Нажмите 'Connect', чтобы подключиться.",
    "Windows": "1. Скачайте и установите клиент OpenVPN с сайта openvpn.net.\n2. Перенесите файл конфигурации .ovpn в папку C:\\Program Files\\OpenVPN\\config.\n3. Запустите OpenVPN GUI от имени администратора.\n4. Найдите ваш профиль и нажмите 'Connect'.",
    "Linux": "1. Убедитесь, что OpenVPN установлен (sudo apt install openvpn).\n2. Откройте терминал и выполните команду: sudo openvpn --config /path/to/your/config.ovpn.\n3. Введите пароль, если потребуется.\n4. VPN будет запущен и работать через терминал.",
    "MacOS": "1. Установите приложение Tunnelblick из Mac App Store или с сайта tunnelblick.net.\n2. Перенесите файл конфигурации .ovpn на устройство.\n3. Откройте файл в Tunnelblick и импортируйте его.\n4. Нажмите 'Connect', чтобы начать работу."
}

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    welcome_message = (
        "Добро пожаловать в HideWay VPN! \n"
        "🔒 Анонимность и безопасность по всему миру!"
        "Мы не ведём логов, обеспечивая вашу конфиденциальность."
        "📍 Доступны серверы в разных странах:"
        "🇺🇸 США  🇨🇦 Канада  🇯🇵 Япония  🇬🇳 Гвинея  🇩🇪 Германия  🇦🇺 Австралия"
        "и многие другие!"
        "🚀 Быстрое подключение и простота использования на всех устройствах. \n"
        "Используйте /help для получения инструкций или /getconfig для получения вашего конфиг файла."
    )
    await message.answer(welcome_message)

@dp.message(Command("help"))
async def help_cmd(message: types.Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=device, callback_data=f"help_{device}")]
            for device in HELP_GUIDES.keys()
        ]
    )
    await message.answer("Выберите устройство, чтобы получить инструкцию:", reply_markup=keyboard)

@dp.callback_query()
async def help_callback(query: types.CallbackQuery):
    device = query.data.replace("help_", "")
    if device in HELP_GUIDES:
        await query.message.answer(f"Инструкция для {device}:\n{HELP_GUIDES[device]}")
    else:
        await query.message.answer("Инструкция не найдена.")
    await query.answer()

@dp.message(Command("getconfig"))
async def get_config_cmd(message: types.Message):
    telegram_id = message.from_user.id
    try:
        cursor.execute("SELECT tariff, config_path FROM users WHERE telegram_id=?", (telegram_id,))
        user = cursor.fetchone()

        if not user:
            await message.answer("Вы не зарегистрированы. Введите /start для начала.")
            return

        tariff, config_path = user
        if not config_path or not os.path.exists(config_path):
            config_path = f"{CONFIG_DIR}/user_{telegram_id}.ovpn"
            result = subprocess.run([GENERATE_SCRIPT, f"user_{telegram_id}"], capture_output=True, text=True)
            if result.returncode == 0:
                cursor.execute("UPDATE users SET config_path=? WHERE telegram_id=?", (config_path, telegram_id))
                conn.commit()
                logger.info(f"Файл конфигурации успешно создан: {config_path}")
            else:
                logger.error(f"Ошибка при генерации файла: {result.stderr}")
                await message.answer("Произошла ошибка при генерации конфигурации. Попробуйте позже.")
                return

        try:
            file_to_send = FSInputFile(config_path)
            await message.answer_document(file_to_send, caption="Вот ваш конфиг для VPN.")
            logger.info(f"Файл успешно отправлен: {config_path}")
        except Exception as e:
            logger.error(f"Ошибка при отправке файла: {e}")
            await message.answer(f"Произошла ошибка при отправке файла. Подробности: {e}")
    except Exception as e:
        logger.exception(f"Ошибка при обработке команды /getconfig для пользователя {telegram_id}: {e}")
        await message.answer("Произошла ошибка при обработке команды. Попробуйте позже.")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
