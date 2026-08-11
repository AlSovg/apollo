# Samba AD DC — проверка AD-ветки синхронизации учёток.
#
# OpenLDAP-мок в dev-оверлее гоняет ветку uid/entryUUID, а прод заказчика —
# Active Directory (sAMAccountName/objectGUID, группы objectClass=group).
# Samba поднимает настоящий AD-совместимый домен без Windows Server.
#
# Разворачивается на ВМ рядом с TrueConf (89.208.86.149): синхронизация
# идёт по сети, как в проде. Наружу публикуются только 389/636.

FROM debian:bookworm-slim

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        samba samba-ad-provision samba-dsdb-modules samba-vfs-modules winbind \
        krb5-user ldb-tools \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh
COPY bootstrap-users.sh /bootstrap-users.sh
RUN chmod +x /entrypoint.sh /bootstrap-users.sh

# LDAP и LDAPS; Kerberos и SMB наружу не нужны — ldap3 ходит simple bind по TLS
EXPOSE 389 636

ENTRYPOINT ["/entrypoint.sh"]
