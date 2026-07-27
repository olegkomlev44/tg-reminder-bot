FROM python:3.11-slim

WORKDIR /app

# Логи сразу пишутся в stdout без буферизации — иначе на некоторых
# хостингах panel логов может вообще ничего не показывать в реальном времени.
ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=UTF-8

# Системные шрифты ставим как дополнительную подстраховку — основной шрифт
# для карточек уже лежит в репозитории в папке fonts/ и работает даже без этого.
RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-dejavu-core \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
