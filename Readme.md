# HideWay VPN + Telegram Bot
# Telegram: **@HideWay_VPN_Bot**

**HideWay** — это инфраструктура, позволяющая предоставлять бесплатные и в перспективе платные VPN-соединения пользователям через Telegram-бота.

## 1. Описание проекта

- **VPN-сервер** на базе OpenVPN.
- **Управление пользователями** и статистикой трафика через базу данных SQLite.
- **Telegram-бот** для выдачи пользователям готовых `.ovpn`-конфигураций, уведомлений о лимите трафика и получения помощи по настройке.

## 2. Ключевые функции

1. **Бесплатный тариф** (10 ГБ/мес) и возможность добавлять платные тарифы в будущем.
2. **Генерация `.ovpn`** конфигурационных файлов для каждого пользователя.
3. **Автоматическое переключение** на бесплатный тариф после истечения оплаченного лимита.
4. **RSA-ключи** для шифрования (Easy-RSA).
5. **Безопасность**:
   - Шифрование AES-256-CBC, аутентификация SHA512.
   - Fail2Ban для защиты от брутфорса.
   - ufw для ограничения входящих подключений.
6. **Telegram-бот**:
   - `/start` — приветственное сообщение.
   - `/help` — инструкции по настройке VPN.
   - `/getconfig` — получение `.ovpn` файла.
   - `/gettraffic` — статистика трафика.

## 3. Структура файлов и каталогов

```plaintext
/etc/openvpn/
  ├─ server/
  │   └─ server.conf         # Конфиг OpenVPN
  ├─ easy-rsa/               # Файлы RSA, pki
  └─ client/                 # Сгенерированные .ovpn-файлы

/opt/tg_bot/
  ├─ bot.py                  # Код Telegram-бота (написать позже)
  ├─ generate_user_config.sh # Скрипт генерации ovpn
  ├─ clean_ovpn.sh           # Скрипт для очистки ovpn от "мусора"
  ├─ base/
  │   └─ vpn_users.db        # SQLite-база данных
  |       └─ users           # Таблица VPN-пользователей
  |       └─ trafic          # Таблица остатка трафика
  └─ logs/                   # Логи работы бота
4. Установка и настройка
4.1 OpenSSH
sudo apt update && sudo apt install openssh-server -y
sudo ufw allow 22
sudo ufw enable
4.2 OpenVPN и Easy-RSA
sudo apt update && sudo apt install openvpn easy-rsa -y
make-cadir /etc/openvpn/easy-rsa
cd /etc/openvpn/easy-rsa
./easyrsa init-pki
./easyrsa build-ca nopass
./easyrsa gen-req server nopass
./easyrsa sign-req server server
# ...
(См. полный список команд в [docs/INSTALL.md] или выше.)
4.3 База данных
sudo apt install sqlite3 -y
cd /opt/tg_bot/base
sqlite3 vpn_users.db
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE,
    username TEXT,
    tariff TEXT,
    traffic_left INTEGER,
    expiry_date TEXT,
    payment_status TEXT,
    config_path TEXT
);

CREATE TABLE traffic (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    date TEXT,
    bytes_used INTEGER,
    FOREIGN KEY(user_id) REFERENCES users(id)
);
4.4 Скрипт генерации конфигураций
#!/bin/bash
CLIENT_NAME=$1
OUTPUT_DIR="/etc/openvpn/client/"
EASY_RSA_DIR="/etc/openvpn/easy-rsa"

cd $EASY_RSA_DIR
./easyrsa build-client-full $CLIENT_NAME nopass
cp $EASY_RSA_DIR/pki/issued/$CLIENT_NAME.crt $OUTPUT_DIR/$CLIENT_NAME.ovpn
4.5 Telegram-бот
    1. Создать бота через BotFather и получить токен. 
    2. Разместить код bot.py в /opt/tg_bot/. 
    3. Создать виртуальное окружение: 
cd /opt/tg_bot
python3 -m venv venv
source venv/bin/activate
pip install aiogram
    4. Запуск бота: 
python bot.py
Или через systemd: 
[Unit]
Description=HideWay Bot VPN Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/tg_bot
ExecStart=/opt/tg_bot/venv/bin/python /opt/tg_bot/bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target