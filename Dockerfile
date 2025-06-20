FROM python:3.12-alpine3.20

# Устанавливаем локаль для вывода дней недели
ENV MUSL_LOCPATH="/usr/share/i18n/locales/musl"

RUN apk --no-cache add \
    musl-locales \
    musl-locales-lang

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем необходимые файлы в контейнер
COPY jira_worklog.py pip-requirements.txt ./

# Устанавливаем необходимые зависимости
RUN pip install --no-cache-dir -r pip-requirements.txt

# Запуск скрипта
ENTRYPOINT ["python", "jira_worklog.py"]