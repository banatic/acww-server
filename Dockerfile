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
#
# SERVERFIX108 / F3.  `--ws-max-size` is uvicorn's OWN ceiling and it defaults to 16 MiB:
# a frame is refused at the protocol layer, before the application sees a byte, so the
# application's own caps (a lobby message, a 4,096-byte relay frame) are the second gate
# rather than the first.  64 KiB is sixteen times the relay's ceiling, which leaves room
# for a future frame contract without leaving room for a memory attack.
#
# ONE WORKER, deliberately and not by omission: the save store's compare-and-swap is a lock
# inside one process, and a second worker would make two writers believe they both won.
# `--no-proxy-headers` is F1 and it is NOT the same switch as ACWW_TRUSTED_PROXIES.
# uvicorn's own ProxyHeadersMiddleware is on by default and rewrites the client address from
# `X-Forwarded-For` whenever the peer is in `--forwarded-allow-ips` -- so the application
# would be deciding whose header to believe using an address taken from that header. The
# service makes that decision itself, from ACWW_TRUSTED_PROXIES, in app/main.py's
# `_client_key`; there is exactly one place it is made and this is how it stays that way.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--ws-max-size", "65536", "--no-proxy-headers"]
