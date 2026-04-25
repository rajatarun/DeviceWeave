FROM python:3.11-slim

# Apache2 + mod_wsgi + build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    apache2 \
    apache2-dev \
    libapache2-mod-wsgi-py3 \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps — requirements.txt first for layer caching
WORKDIR /app
COPY src/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir \
    -r /app/requirements.txt \
    boto3 \
    python-kasa

# Application source
COPY src/ /app/

# WSGI adapter, table initialiser, and Apache config
COPY docker/wsgi_handler.py  /app/wsgi_handler.py
COPY docker/init_tables.py   /app/init_tables.py
COPY docker/apache.conf     /etc/apache2/sites-available/deviceweave.conf

# Enable WSGI module and our vhost; disable default
RUN a2enmod wsgi \
 && a2ensite deviceweave \
 && a2dissite 000-default

ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 80

CMD ["apache2ctl", "-D", "FOREGROUND"]
