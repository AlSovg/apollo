#!/bin/bash
# Провижининг домена при первом старте, дальше — просто запуск.
# Данные живут в /var/lib/samba (volume): пересоздание контейнера
# не пересоздаёт домен.
set -euo pipefail

REALM="${AD_REALM:-APOLLO.TEST}"
DOMAIN="${AD_DOMAIN:-APOLLO}"
ADMIN_PASS="${AD_ADMIN_PASS:?Задайте AD_ADMIN_PASS}"

if [ ! -f /var/lib/samba/private/sam.ldb ]; then
    echo "[ad] домен ${REALM}: первый запуск, провижининг"
    rm -f /etc/samba/smb.conf
    samba-tool domain provision \
        --realm="${REALM}" \
        --domain="${DOMAIN}" \
        --server-role=dc \
        --dns-backend=SAMBA_INTERNAL \
        --adminpass="${ADMIN_PASS}"
    cp /var/lib/samba/private/krb5.conf /etc/krb5.conf

    # Пароли тестовых учёток стенда не должны протухать посреди демо
    samba-tool domain passwordsettings set --max-pwd-age=0 --complexity=off

    /bootstrap-users.sh

    # smb.conf живёт вне volume: без копии пересозданный контейнер
    # стартовал бы как standalone-сервер, а не контроллер домена
    cp /etc/samba/smb.conf /var/lib/samba/smb.conf.provisioned
else
    echo "[ad] домен уже развёрнут, обычный запуск"
    if [ -f /var/lib/samba/smb.conf.provisioned ]; then
        cp /var/lib/samba/smb.conf.provisioned /etc/samba/smb.conf
        cp /var/lib/samba/private/krb5.conf /etc/krb5.conf
    fi
fi

exec samba --interactive --debug-stdout
