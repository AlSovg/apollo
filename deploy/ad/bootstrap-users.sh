#!/bin/bash
# Тестовые учётки и группы домена. Структура повторяет OpenLDAP-мок,
# чтобы обе ветки синхронизации проверялись на одинаковом наборе:
# админ, два инженера, наблюдатель и сотрудник без ролей.
set -euo pipefail

BASE_DN="$(python3 - "$AD_REALM" <<'PY'
import sys
print(",".join(f"DC={part}" for part in sys.argv[1].lower().split(".")))
PY
)"
SAM_DB=/var/lib/samba/private/sam.ldb
USER_PASS="${AD_USER_PASS:?Задайте AD_USER_PASS}"

echo "[ad] создаю OU, группы и пользователей в ${BASE_DN}"

samba-tool ou add "OU=apollo-people,${BASE_DN}"
samba-tool ou add "OU=apollo-groups,${BASE_DN}"

for group in platform-admins noc-engineers service-owners; do
    samba-tool group add "$group" --groupou="OU=apollo-groups"
done

add_user() { # логин, имя, фамилия, почта
    samba-tool user add "$1" "$USER_PASS" \
        --userou="OU=apollo-people" \
        --given-name="$2" --surname="$3" \
        --mail-address="$4"
}

add_user ad.admin  "Администратор" "Платформы"  "ad.admin@apollo.test"
add_user ad.duty   "Дежурный"      "Смены"      "ad.duty@apollo.test"
add_user ad.eng    "Инженер"       "Сети"       "ad.eng@apollo.test"
add_user ad.viewer "Владелец"      "Сервиса"    "ad.viewer@apollo.test"
add_user ad.nobody "Сотрудник"     "Без ролей"  "ad.nobody@apollo.test"

samba-tool group addmembers platform-admins ad.admin
samba-tool group addmembers noc-engineers   ad.duty,ad.eng
samba-tool group addmembers service-owners  ad.viewer

# Адрес TrueConf живёт в ipPhone (ldap_trueconf_attribute) — samba-tool
# этот атрибут не выставляет, поэтому ldbmodify по локальной базе.
# DN берётся поиском: samba-tool собирает CN из имени и фамилии
# («CN=Дежурный Смены»), а не из логина — правка по CN=<логин>
# молча меняет ноль записей.
set_ipphone() {
    local dn
    dn="$(ldbsearch -H "$SAM_DB" "(sAMAccountName=$1)" dn | awk '/^dn: /{sub(/^dn: /,""); print; exit}')"
    if [ -z "$dn" ]; then
        echo "[ad] учётка $1 не найдена, ipPhone не задан" >&2
        return 1
    fi
    ldbmodify -H "$SAM_DB" <<LDIF
dn: ${dn}
changetype: modify
replace: ipPhone
ipPhone: $2
LDIF
}

set_ipphone ad.duty user2
set_ipphone ad.eng  user3

echo "[ad] готово: 5 пользователей, 3 группы"
