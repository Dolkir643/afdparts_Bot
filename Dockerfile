FROM ghcr.io/hassio-addons/base-python:14.0.0

WORKDIR /usr/src/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY parser.py tg_bot.py ./
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
