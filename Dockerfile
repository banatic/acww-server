# The ACWW online service.  Built for Synology Container Manager, which runs an image the
# operator builds once and then forgets; everything here is pinned for that reason.
#
# NOTHING FROM THE GAME IS IN THIS IMAGE.  No ROM, no extract/, no asset, not even the
# sample save the tests use -- .dockerignore keeps tests/ out of the context.  The image is
# six pinned packages and the `app` package, and the only game-derived thing in it is the
# five constants of the ROM's save acceptance test in app/savecheck.py.
FROM python:3.12-slim

# Byte-for-byte logs at the moment they happen: Container Manager shows stdout and a
# buffered service looks dead for the first four kilobytes of its life.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    ACWW_DATA_DIR=/data

WORKDIR /srv

COPY requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r /srv/requirements.txt

COPY app /srv/app

# Non-root.  The uid is FIXED at 10001 rather than left to the distro, because the data
# volume's ownership on the NAS has to match it and an operator cannot chown to a number
# that moves between rebuilds.  /data is created and handed over here so that a docker
# NAMED volume inherits the right owner; a bind mount keeps the host's ownership instead,
# which is why README-ko.md tells the operator to chown the share to 10001.
RUN useradd --uid 10001 --create-home --home-dir /home/acww acww \
    && mkdir -p /data \
    && chown -R acww:acww /data /srv
USER acww

EXPOSE 8080

# TLS, the public hostname and the certificate are the NAS reverse proxy's job; this
# speaks plain HTTP on one port and nothing else.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
