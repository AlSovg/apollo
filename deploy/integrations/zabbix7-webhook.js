// Media type «Apollo» для Zabbix 7.0: отправка события в /api/v1/ingest/zabbix.
//
// Скрипт ничего не решает по существу: он собирает поля из макросов и отдаёт
// их как есть. Нормализация, дедупликация и критичность — на стороне apollo,
// иначе логика расползётся по конфигурации мониторинга.
//
// Параметры задаются в Parameters媒 медиатипа (список — в README.md рядом).
// Ошибка бросается исключением: Zabbix повторит отправку по Attempts.

var Apollo = {
    params: null,

    // Поля, которые ждёт адаптер zabbix. Пустые не отправляем: пустая строка
    // в поле хоста выглядит как значение, а отсутствие — как отсутствие.
    buildPayload: function () {
        var payload = {};
        var fields = [
            'event_id', 'event_nseverity', 'severity', 'status', 'event_value',
            'host', 'host_technical_name', 'host_ip', 'hostgroup',
            'trigger_id', 'trigger_name', 'trigger_expression',
            'message', 'item_lastvalue'
        ];

        for (var i = 0; i < fields.length; i++) {
            var value = Apollo.params[fields[i]];
            if (value !== undefined && value !== null && value !== '' && value.indexOf('{') !== 0) {
                payload[fields[i]] = value;
            }
        }

        payload.timestamp = Apollo.eventTime();

        var tags = Apollo.parseTags();
        if (tags !== null) {
            payload.event_tags = tags;
        }

        return payload;
    },

    // У восстановления собственное время: если брать время проблемы, инцидент
    // закроется задним числом и длительность станет отрицательной
    eventTime: function () {
        var resolved = (Apollo.params.status || '').toUpperCase() === 'RESOLVED';
        if (resolved && Apollo.params.recovery_date && Apollo.params.recovery_date.indexOf('{') !== 0) {
            return Apollo.params.recovery_date + ' ' + Apollo.params.recovery_time;
        }
        return Apollo.params.date + ' ' + Apollo.params.time;
    },

    // {EVENT.TAGSJSON} даёт массив объектов, {EVENT.TAGS} — строку через запятую.
    // apollo понимает оба вида, но JSON нужно распарсить здесь.
    parseTags: function () {
        var raw = Apollo.params.event_tags;
        if (!raw || raw.indexOf('{') === 0) {
            return null;
        }
        if (raw.charAt(0) !== '[') {
            return raw;
        }
        try {
            return JSON.parse(raw);
        } catch (error) {
            Zabbix.log(4, '[apollo] теги не разобраны, отправляю строкой: ' + raw);
            return raw;
        }
    },

    send: function (payload) {
        var request = new HttpRequest();
        request.addHeader('Content-Type: application/json');
        request.addHeader('X-Webhook-Token: ' + Apollo.params.token);

        // Подпись опциональна: встроенная hmac() есть не во всех сборках Zabbix.
        // Если параметр не задан, apollo проверит только токен.
        if (Apollo.params.hmac_secret && Apollo.params.hmac_secret.indexOf('{') !== 0) {
            request.addHeader(
                'X-Webhook-Signature: ' + hmac('sha256', Apollo.params.hmac_secret, payload)
            );
        }

        var response = request.post(Apollo.params.url, payload);
        var status = request.getStatus();

        // Приём отвечает 202: событие записано в raw_events, разбор идёт следом
        if (status < 200 || status >= 300) {
            throw 'apollo вернул ' + status + ': ' + response;
        }

        return response;
    }
};

try {
    Apollo.params = JSON.parse(value);

    if (!Apollo.params.url || Apollo.params.url.indexOf('{') === 0) {
        throw 'не задан параметр url';
    }
    if (!Apollo.params.token || Apollo.params.token.indexOf('{') === 0) {
        throw 'не задан параметр token (макрос {$APOLLO_TOKEN})';
    }

    var body = JSON.stringify(Apollo.buildPayload());
    Zabbix.log(4, '[apollo] отправка: ' + body);

    var answer = Apollo.send(body);
    Zabbix.log(4, '[apollo] ответ: ' + answer);

    return answer;
} catch (error) {
    Zabbix.log(3, '[apollo] отправка не удалась: ' + error);
    throw 'apollo: ' + error;
}
