#!/usr/bin/env bash

# Проверка наличия аргумента
if [ -z "$1" ]; then
  echo "Usage: $0 <ovpn_file>"
  exit 1
fi

OVPN_FILE="$1"

# Проверка существования файла
if [ ! -f "$OVPN_FILE" ]; then
  echo "Error: File $OVPN_FILE does not exist."
  exit 1
fi

# Удаление лишней информации между <cert> и -----BEGIN CERTIFICATE-----
awk '
BEGIN { in_cert_block = 0; }
# Начало блока <cert>
/<cert>/ {
    print; 
    in_cert_block = 1; 
    next;
}
# Начало действительного сертификата
/-----BEGIN CERTIFICATE-----/ {
    if (in_cert_block) {
        print;
        in_cert_block = 0;  # Завершаем обработку лишних данных
    }
    next;
}
# Конец блока <cert>
/<\/cert>/ {
    print; 
    next;
}
# Печатаем строки только если они не относятся к лишней информации
!in_cert_block {
    print;
}
' "$OVPN_FILE" > "${OVPN_FILE}.tmp"

# Перезаписываем оригинальный файл
mv "${OVPN_FILE}.tmp" "$OVPN_FILE"

echo "File $OVPN_FILE cleaned successfully."
